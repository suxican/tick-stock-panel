import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.position_t_market import PositionTMarket, market_time_ms, normalize_bars
from app.strategy.position_t import DEFAULT_CONFIG


def test_bar_time_semantics_units_and_no_incomplete_bar():
    now = datetime(2026, 10, 16, 9, 31, 5, tzinfo=CN_TZ)
    rows = [{"datetime": "2026-10-16T09:30:00", "open": 10, "high": 10, "low": 10,
             "close": 10, "volume": 5, "amount": 5000},
            {"datetime": "2026-10-16T09:31:00", "open": 10, "high": 10, "low": 10,
             "close": 10, "volume": 1, "amount": 1000}]
    bars = normalize_bars(rows, now, "start")
    assert len(bars) == 1 and bars[0]["volume_shares"] == 500
    assert bars[0]["end"] == "2026-10-16T09:31:00+08:00"
    assert normalize_bars(rows, now, "unknown") == []
    assert market_time_ms(None) is None
    assert market_time_ms(now.timestamp() * 1000) == now


def test_security_metadata_unknown_status_and_stale_book_fail_closed(tmp_path):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
        get_daily=lambda *a, **kw: pl.DataFrame({"date": ["2026-10-15"], "volume": [2400]}),
        resolve_asset_type=lambda s: "stock")
    market = PositionTMarket(repo, None, None, None)
    market._instruments = {"600000.SH": {"symbol": "600000.SH", "name": "测试", "listing_date": "1999-11-10"}}
    now = datetime(2026, 10, 16, 10, 20, tzinfo=CN_TZ)
    raw = {"source": "fixture", "last_price": 10, "prev_close": 10, "timestamp": now.timestamp() * 1000, "received_ts": now.timestamp() * 1000}
    quote = market._quote({"symbol": "600000.SH", "board": "main"}, raw,
        {"bid_prices": [9.99], "ask_prices": [10.01]}, now, DEFAULT_CONFIG)
    assert quote["trade_status"] == "unknown"
    assert quote["limit_up_price"] == 11
    assert any("盘口时间" in x for x in quote["data_errors"])
    assert any("交易状态" in x for x in quote["data_errors"])
    assert market.trading_day(datetime(2026, 10, 18).date()) is False
    assert market.trading_day(datetime(2027, 1, 4).date()) is None


def _market_context(tmp_path, monkeypatch):
    now = datetime(2026, 10, 16, 10, 20, 5, tzinfo=CN_TZ)
    stamp = now.timestamp() * 1000
    raw = {"symbol": "600000.SH", "source": "fixture", "last_price": 10, "prev_close": 10,
           "timestamp": stamp, "received_ts": stamp, "change_pct": 0, "trade_status": "trading",
           "limit_up_price": 11, "limit_down_price": 9}
    quote_service = SimpleNamespace(status=lambda: {"enabled": True, "paused": False, "realtime_allowed": True},
        get_market_records=lambda: {"600000.SH": raw, "000001.SH": {**raw, "symbol": "000001.SH"}})
    depth = SimpleNamespace(get_order_books=Mock(return_value={"600000.SH": {
        "timestamp": stamp, "bid_prices": [9.99], "ask_prices": [10.01]}}))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
        get_instruments=lambda: pl.DataFrame({"symbol": ["600000.SH"], "listing_date": ["1999-11-10"]}),
        get_daily=lambda *a, **kw: pl.DataFrame({"date": ["2026-10-15"] * 20, "volume": [24000] * 20}),
        resolve_asset_type=lambda s: "stock")
    adapter = PositionTMarket(repo, quote_service, depth, None)
    adapter.sectors = SimpleNamespace(build_snapshots=lambda *a, **kw: {
        "index:000001.SH": {"valid": True, "window_changes": {5: .001}},
        "industry:test": {"valid": True, "window_changes": {5: .002}, "coverage_ratio": 1},
    })
    fetch = Mock(return_value=pl.DataFrame({"symbol": ["600000.SH"], "datetime": ["2026-10-16T10:20:00"],
        "open": [10.], "high": [10.], "low": [10.], "close": [10.], "volume": [100], "amount": [100000.]}))
    monkeypatch.setattr("app.services.kline_sync.intraday_monitor_support", lambda c: {"available": True, "max_symbols": 100})
    monkeypatch.setattr("app.services.kline_sync.fetch_intraday_monitor_batch", fetch)
    monkeypatch.setattr("app.services.preferences.get_minute_data_provider", lambda: "tickflow")
    pos = {"account_id": "a", "symbol": "600000.SH", "board": "main", "benchmark_symbol": "000001.SH", "sector_id": "industry:test"}
    accounts = {"a": {"config": {**DEFAULT_CONFIG, "minute_timestamp_semantics": "end"}}}
    return adapter, pos, accounts, fetch, now


def test_collect_batches_and_caches_minutes_respects_global_pause(tmp_path, monkeypatch):
    adapter, pos, accounts, fetch, now = _market_context(tmp_path, monkeypatch)
    result = adapter.collect([pos], accounts, now)[("a", "600000.SH")]
    assert result["quote"]["data_errors"] == []
    assert result["bars"][0]["volume_shares"] == 10000
    assert result["environment"]["market_return_5m"] == .001
    adapter.collect([pos], accounts, now + timedelta(seconds=3))
    assert fetch.call_count == 1
    adapter.quotes.status = lambda: {"enabled": False}
    paused = adapter.collect([pos], accounts, now)[("a", "600000.SH")]
    assert paused["bars"] == []
    assert adapter.depth.get_order_books.call_count == 2
    assert any("全局实时" in x for x in paused["quote"]["data_errors"])


@pytest.mark.parametrize("stamp", [
    "2026-10-16T09:29:59+08:00", "2026-10-16T11:30:00+08:00",
    "2026-10-16T14:57:00+08:00", "2026-10-16T04:00:00+00:00",
    "2026-10-17T10:00:00+08:00", "2026-10-01T10:00:00+08:00",
    "2027-01-04T10:00:00+08:00",
])
def test_closed_market_never_reads_quotes_metadata_or_history(tmp_path, monkeypatch, stamp):
    adapter, pos, accounts, fetch, _ = _market_context(tmp_path, monkeypatch)
    readers = [
        (adapter.repo, "get_instruments"), (adapter.repo, "get_daily"),
        (adapter.quotes, "status"), (adapter.quotes, "get_market_records"),
        (adapter.depth, "get_order_books"), (adapter.sectors, "build_snapshots"),
    ]
    for obj, name in readers:
        setattr(obj, name, Mock(side_effect=AssertionError("must not read market data")))
    assert adapter.collect([pos], accounts, datetime.fromisoformat(stamp)) == {}
    fetch.assert_not_called()
    for obj, name in readers:
        getattr(obj, name).assert_not_called()


@pytest.mark.parametrize("semantics", ["start", "end"])
def test_delayed_complete_minute_retries_once_after_grace(tmp_path, monkeypatch, semantics):
    adapter, pos, accounts, fetch, now = _market_context(tmp_path, monkeypatch)
    accounts["a"]["config"]["minute_timestamp_semantics"] = semantics
    stamp = now.replace(second=0, tzinfo=None) - timedelta(minutes=semantics == "start")
    latest = fetch.return_value.with_columns(pl.lit(stamp.isoformat()).alias("datetime"))
    delayed = latest.with_columns(pl.lit((stamp-timedelta(minutes=1)).isoformat()).alias("datetime"))
    fetch.side_effect = [delayed, latest]
    first = adapter.collect([pos], accounts, now)[("a", pos["symbol"])]
    assert first["bars"][-1]["end"] == "2026-10-16T10:19:00+08:00"
    adapter.collect([pos], accounts, now + timedelta(seconds=5))
    assert fetch.call_count == 1
    ready = adapter.collect([pos], accounts, now + timedelta(seconds=10))[("a", pos["symbol"])]
    assert ready["bars"][-1]["end"] == "2026-10-16T10:20:00+08:00"
    for seconds in (15, 30, 50):
        adapter.collect([pos], accounts, now + timedelta(seconds=seconds))
    assert fetch.call_count == 2


def test_missing_minutes_do_not_retry_without_bound(tmp_path, monkeypatch):
    adapter, pos, accounts, fetch, now = _market_context(tmp_path, monkeypatch)
    fetch.return_value = pl.DataFrame()
    for seconds in (0, 5, 10, 15, 30, 50):
        result = adapter.collect([pos], accounts, now + timedelta(seconds=seconds))[("a", pos["symbol"])]
        assert result["bars"] == []
    assert fetch.call_count == 2
    adapter.collect([pos], accounts, now + timedelta(minutes=1))
    assert fetch.call_count == 3


@pytest.mark.parametrize("semantics", ["end", "unknown"])
def test_complete_or_unconfirmed_minute_semantics_do_not_need_retry(tmp_path, monkeypatch, semantics):
    adapter, pos, accounts, fetch, now = _market_context(tmp_path, monkeypatch)
    accounts["a"]["config"]["minute_timestamp_semantics"] = semantics
    for seconds in (0, 10, 25, 50):
        adapter.collect([pos], accounts, now + timedelta(seconds=seconds))
    assert fetch.call_count == 1


def test_failed_retry_does_not_repeat_or_cross_source(tmp_path, monkeypatch):
    adapter, pos, accounts, fetch, now = _market_context(tmp_path, monkeypatch)
    fetch.side_effect = [pl.DataFrame(), RuntimeError("provider unavailable")]
    adapter.collect([pos], accounts, now)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        adapter.collect([pos], accounts, now + timedelta(seconds=10))
    result = adapter.collect([pos], accounts, now + timedelta(seconds=20))[("a", pos["symbol"])]
    assert result["bars"] == []
    assert fetch.call_count == 2


def test_raw_quote_accessor_keeps_market_source_time_and_returns_copy(monkeypatch):
    from app.services.quote_service import QuoteService

    service = QuoteService()
    monkeypatch.setattr("app.services.quote_service._persist_last_fetch", lambda value: None)
    monkeypatch.setattr(service, "_broadcast_quote_updated", lambda: None)
    record = {"symbol": "600000.SH", "last_price": 10, "prev_close": 10, "timestamp": 1000}
    service._process_full_market_records([record], t0=time.perf_counter(), now_ts=time.perf_counter(),
        final_boundary_ms=9_999_999, source="fixture_provider")
    snapshot = service.get_market_records(["600000.SH"])
    assert snapshot["600000.SH"]["source"] == "fixture_provider"
    assert snapshot["600000.SH"]["timestamp"] == 1000
    assert snapshot["600000.SH"]["received_ts"] > 1000
    snapshot["600000.SH"]["last_price"] = 99
    assert service.get_market_records()["600000.SH"]["last_price"] == 10
