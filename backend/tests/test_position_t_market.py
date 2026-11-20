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


def test_beijing_theoretical_limits_stay_inside_the_band(tmp_path):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
        get_daily=lambda *a, **kw: pl.DataFrame({"date": ["2026-10-15"], "volume": [2400]}),
        resolve_asset_type=lambda s: "stock")
    market = PositionTMarket(repo, None, None, None)
    market._instruments = {"920096.BJ": {"symbol": "920096.BJ", "name": "北交所样本", "listing_date": "2023-05-10"}}
    now = datetime(2026, 10, 20, 10, 20, tzinfo=CN_TZ)
    raw = {"source": "fixture", "last_price": 30, "prev_close": 27.52,
           "timestamp": now.timestamp() * 1000, "received_ts": now.timestamp() * 1000}
    quote = market._quote({"symbol": "920096.BJ", "board": "bse"}, raw,
        {"bid_prices": [29.99], "ask_prices": [30.01]}, now, DEFAULT_CONFIG)
    # 27.52×1.3=35.776 向下取整, 27.52×0.7=19.264 向上取整
    assert quote["limit_up_price"] == 35.77
    assert quote["limit_down_price"] == 19.27


REF_NOW = datetime(2026, 10, 16, 10, 20, tzinfo=CN_TZ)
LIMITS = {"600000.SH": {"limit_up": 10.95, "limit_down": 8.96, "no_limit": False}}


def _ref_quote(tmp_path, monkeypatch, reference, *, raw=None, book=None, listing="1999-11-10"):
    """day_reference 打桩后的单只报价: 上一交易日收盘 9.955, 本地理论价 10.95/8.96。"""
    from app.services import market_reference

    monkeypatch.setattr(market_reference, "day_reference",
                        lambda day: {"price_limits": None, "st": None, "suspensions": None, **reference})
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
        get_daily=lambda *a, **kw: pl.DataFrame({"date": ["2026-10-15"], "volume": [2400]}),
        resolve_asset_type=lambda s: "stock")
    market = PositionTMarket(repo, None, None, None)
    market._instruments = {"600000.SH": {"symbol": "600000.SH", "name": "测试", "listing_date": listing}}
    stamp = REF_NOW.timestamp() * 1000
    raw = {"source": "fixture", "last_price": 10, "prev_close": 9.955, "timestamp": stamp, "received_ts": stamp,
           **(raw or {})}
    book = {"bid_prices": [9.99], "ask_prices": [10.01], "timestamp": stamp, **(book or {})}
    return market._quote({"symbol": "600000.SH", "board": "main"}, raw, book, REF_NOW, DEFAULT_CONFIG)


def test_exchange_daily_limits_used_when_quote_lacks(tmp_path, monkeypatch):
    limits = {"600000.SH": {"limit_up": 10.5, "limit_down": 9.4, "no_limit": False}}  # 除权日交易所价
    quote = _ref_quote(tmp_path, monkeypatch, {"price_limits": limits})
    assert (quote["limit_up_price"], quote["limit_down_price"]) == (10.5, 9.4)
    assert quote["price_limit_source"] == "exchange_daily"
    assert not any("理论价" in x for x in quote["data_errors"])


def test_quote_limits_take_precedence_over_exchange_daily(tmp_path, monkeypatch):
    quote = _ref_quote(tmp_path, monkeypatch, {"price_limits": LIMITS},
                       raw={"limit_up_price": 10.9, "limit_down_price": 9.0})
    assert (quote["limit_up_price"], quote["price_limit_source"]) == (10.9, "quote")


def test_exchange_no_limit_blocks(tmp_path, monkeypatch):
    limits = {"600000.SH": {"limit_up": None, "limit_down": None, "no_limit": True}}
    quote = _ref_quote(tmp_path, monkeypatch, {"price_limits": limits})
    assert any("无涨跌幅限制" in x for x in quote["data_errors"])


def test_theoretical_limits_without_reference_stay_observation_only(tmp_path, monkeypatch):
    quote = _ref_quote(tmp_path, monkeypatch, {})
    assert quote["price_limit_source"] == "theoretical"
    assert any("理论价仅作观察" in x for x in quote["data_errors"])


def test_daily_status_trading_when_not_suspended(tmp_path, monkeypatch):
    quote = _ref_quote(tmp_path, monkeypatch, {"price_limits": LIMITS, "suspensions": {"000002.SZ": None}})
    assert (quote["trade_status"], quote["trade_status_basis"]) == ("trading", "daily")
    assert quote["data_errors"] == []


def test_daily_status_suspended_when_listed(tmp_path, monkeypatch):
    quote = _ref_quote(tmp_path, monkeypatch, {"price_limits": LIMITS, "suspensions": {"600000.SH": "09:30-10:30"}})
    assert (quote["trade_status"], quote["trade_status_basis"]) == ("suspended", "daily")
    assert any("停牌名单" in x for x in quote["data_errors"])


def test_status_unknown_without_suspension_table(tmp_path, monkeypatch):
    quote = _ref_quote(tmp_path, monkeypatch, {"price_limits": LIMITS})
    assert (quote["trade_status"], quote["trade_status_basis"]) == ("unknown", "none")
    assert any("交易状态" in x for x in quote["data_errors"])


def test_status_unknown_without_listing_date(tmp_path, monkeypatch):
    quote = _ref_quote(tmp_path, monkeypatch, {"price_limits": LIMITS, "suspensions": {}}, listing=None)
    assert quote["trade_status"] == "unknown"


def test_explicit_source_status_is_intraday(tmp_path, monkeypatch):
    quote = _ref_quote(tmp_path, monkeypatch, {"suspensions": {}}, raw={"trade_status": "suspended"})
    assert (quote["trade_status"], quote["trade_status_basis"]) == ("suspended", "intraday")


def test_both_sides_empty_flags_suspected_halt(tmp_path, monkeypatch):
    quote = _ref_quote(tmp_path, monkeypatch, {"price_limits": LIMITS, "suspensions": {}},
                       book={"bid_prices": [0], "ask_prices": []})
    assert any("疑似盘中停牌" in x for x in quote["data_errors"])


def test_one_side_empty_no_halt_flag(tmp_path, monkeypatch):
    quote = _ref_quote(tmp_path, monkeypatch, {"price_limits": LIMITS, "suspensions": {}},
                       book={"bid_prices": [10.95], "ask_prices": [0]})  # 封涨停
    assert not any("疑似盘中停牌" in x for x in quote["data_errors"])


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


def test_partial_quote_publication_keeps_untouched_asset_records(monkeypatch):
    from app.services.quote_service import QuoteService

    service = QuoteService()
    service._repo = SimpleNamespace(
        get_index_symbol_set=lambda: {"000001.SH"},
        get_etf_instruments=lambda: pl.DataFrame({"symbol": ["159558.SZ", "510300.SH"]}),
    )
    monkeypatch.setattr("app.services.quote_service._persist_last_fetch", lambda value: None)
    monkeypatch.setattr(service, "_broadcast_quote_updated", lambda: None)
    original = [
        {"symbol": symbol, "last_price": 10, "timestamp": 1000}
        for symbol in ["600000.SH", "600519.SH", "159558.SZ", "510300.SH", "000001.SH"]
    ]
    service._process_full_market_records(original, t0=0, now_ts=0,
                                        final_boundary_ms=9_999_999, source="first")
    old = service.get_market_records()
    updated = [{"symbol": "600519.SH", "last_price": 11, "timestamp": 2000},
               {"symbol": "159558.SZ", "last_price": 12, "timestamp": 2000}]
    service._process_full_market_records(updated, t0=0, now_ts=0, source="second",
                                        partial_assets={"stock", "etf"}, replace_index_cache=False,
                                        final_boundary_ms=9_999_999)
    result = service.get_market_records()
    for symbol in ["600000.SH", "510300.SH", "000001.SH"]:
        assert result[symbol] == old[symbol]
    assert result["600519.SH"]["last_price"] == 11
    assert result["159558.SZ"]["source"] == "second"
    service._process_full_market_records(updated, t0=0, now_ts=0, source="third",
                                        final_boundary_ms=9_999_999)
    assert set(service.get_market_records()) == {"600519.SH", "159558.SZ"}


def test_collect_uses_directly_fetched_position_quotes(tmp_path, monkeypatch):
    """全市场快照到手已晚 5-15 秒 (猫爪 tick 实测); 做T 持仓单独拉取, 不读共享快照的旧报价。"""
    adapter, pos, accounts, _fetch, now = _market_context(tmp_path, monkeypatch)
    cached = adapter.quotes.get_market_records()
    stale = {**cached["600000.SH"], "timestamp": (now - timedelta(seconds=14)).timestamp() * 1000}
    adapter.quotes.get_market_records = lambda: {**cached, "600000.SH": stale}
    direct = {**cached["600000.SH"], "last_price": 10.02, "source": "direct"}
    adapter.quotes.fetch_symbol_records = Mock(return_value={"600000.SH": direct})
    quote = adapter.collect([pos], accounts, now)[("a", "600000.SH")]["quote"]
    assert adapter.quotes.fetch_symbol_records.call_args_list[0].args == (["600000.SH"],)
    assert quote["price"] == 10.02 and quote["source"] == "direct"
    assert quote["data_errors"] == []


def test_collect_keeps_shared_quotes_when_direct_fetch_unavailable(tmp_path, monkeypatch):
    adapter, pos, accounts, _fetch, now = _market_context(tmp_path, monkeypatch)
    adapter.quotes.fetch_symbol_records = Mock(return_value={})
    quote = adapter.collect([pos], accounts, now)[("a", "600000.SH")]["quote"]
    assert quote["source"] == "fixture" and quote["data_errors"] == []
    adapter.quotes.status = lambda: {"enabled": False}
    adapter.quotes.fetch_symbol_records.reset_mock()
    adapter.collect([pos], accounts, now)
    adapter.quotes.fetch_symbol_records.assert_not_called()  # 全局行情暂停时不另行取数


def test_fetch_symbol_records_uses_plugin_without_touching_shared_cache(monkeypatch):
    from app.data_providers import custom as custom_sources
    from app.services import preferences
    from app.services.quote_service import QuoteService

    service = QuoteService()
    provider = SimpleNamespace(get_realtime_symbols=Mock(return_value=[
        {"symbol": "600000.SH", "last_price": 10, "timestamp": 1000},
        {"symbol": "600001.SH", "last_price": 9, "timestamp": 1000},
    ]))
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "plugin_x")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, ds: name == "plugin_x")
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: provider)
    out = service.fetch_symbol_records(["600000.SH", "600000.SH"])
    provider.get_realtime_symbols.assert_called_once_with(["600000.SH"], asset_type="stock")
    assert set(out) == {"600000.SH"}  # 只返回所请求的代码
    assert out["600000.SH"]["source"] == "plugin_x" and out["600000.SH"]["timestamp"] == 1000
    assert out["600000.SH"]["received_ts"] > 1000
    assert service.get_market_records() == {}  # 不写共享缓存, 不影响收盘定版

    provider.get_realtime_symbols.side_effect = RuntimeError("down")
    assert service.fetch_symbol_records(["600000.SH"]) == {}
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "tickflow")
    assert service.fetch_symbol_records(["600000.SH"]) == {}  # 无按代码接口的源沿用共享快照


def test_collect_checks_freshness_after_fetching_data(tmp_path, monkeypatch):
    """拉取持仓报价与盘口需要数秒; 时效须以取完数据后的时刻判断, 晚于轮次开始的数据不算异常。"""
    adapter, pos, accounts, _fetch, now = _market_context(tmp_path, monkeypatch)
    later = (now + timedelta(seconds=3)).timestamp() * 1000
    cached = adapter.quotes.get_market_records()
    adapter.quotes.fetch_symbol_records = Mock(return_value={"600000.SH": {**cached["600000.SH"], "timestamp": later}})
    adapter.depth.get_order_books = Mock(return_value={"600000.SH": {
        "timestamp": later, "bid_prices": [9.99], "ask_prices": [10.01]}})
    quote = adapter.collect([pos], accounts, now, clock=lambda: now + timedelta(seconds=4))[("a", "600000.SH")]["quote"]
    assert quote["data_errors"] == []
    assert quote["computed_at"] == (now + timedelta(seconds=4)).isoformat()
    stale = adapter.collect([pos], accounts, now)[("a", "600000.SH")]["quote"]  # 不传时钟时按轮次开始判断
    assert any("盘口时间" in x for x in stale["data_errors"])


def test_collect_fetches_sector_members_and_benchmark_directly(tmp_path, monkeypatch):
    """全市场快照约 15 秒一轮, 大盘与板块环境的成员和基准指数同样须按代码直接取最新。"""
    adapter, pos, accounts, _fetch, now = _market_context(tmp_path, monkeypatch)
    later = (now + timedelta(seconds=2)).timestamp() * 1000
    stale = (now - timedelta(seconds=15)).timestamp() * 1000
    cached = adapter.quotes.get_market_records()
    base = {**cached["600000.SH"], "timestamp": stale}
    adapter.quotes.get_market_records = lambda: {"600000.SH": base, "600001.SH": {**base, "symbol": "600001.SH"},
                                                 "000001.SH": {**base, "symbol": "000001.SH"}}

    def direct(symbols, asset_type="stock"):
        return {s: {**base, "symbol": s, "timestamp": later} for s in symbols}

    adapter.quotes.fetch_symbol_records = Mock(side_effect=direct)
    adapter.sectors.members_for = Mock(return_value={"600001.SH"})
    seen = {}

    def build(stock_df, index_df, targets, windows, *, now):
        seen.update(stocks=set(stock_df["symbol"]), indices=set(index_df["symbol"]), now=now,
                    targets={t["key"] for t in targets})
        return {}

    adapter.sectors.build_snapshots = build
    adapter.collect([pos], accounts, now, clock=lambda: now + timedelta(seconds=3))
    assert {tuple(c.args[0]) for c in adapter.sectors.members_for.call_args_list} == {("industry:test",)}
    calls = adapter.quotes.fetch_symbol_records.call_args_list
    assert calls[0].args == (["600000.SH", "600001.SH"],)
    assert calls[1].args == (["000001.SH"],) and calls[1].kwargs == {"asset_type": "index"}
    # 晚于轮次开始的直接取数参与聚合 (以取完数据的时刻判断), 共享快照里的旧报价不参与
    assert seen["stocks"] == {"600000.SH", "600001.SH", "000001.SH"}
    assert seen["targets"] == {"index:000001.SH", "industry:test"}
    assert seen["now"] == (now + timedelta(seconds=3)).timestamp()


def test_fetch_symbol_records_index_path(monkeypatch):
    from app.data_providers import custom as custom_sources
    from app.services import preferences
    from app.services.quote_service import QuoteService

    provider = SimpleNamespace(get_realtime_indices=Mock(return_value=[{"symbol": "000001.SH", "timestamp": 1000}]))
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "plugin_x")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, ds: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: provider)
    out = QuoteService().fetch_symbol_records(["000001.SH"], asset_type="index")
    provider.get_realtime_indices.assert_called_once_with(["000001.SH"])
    assert out["000001.SH"]["source"] == "plugin_x"


def test_sector_members_for_known_keys_only(tmp_path):
    from app.services.sector_monitor import SectorMonitorService

    service = SectorMonitorService(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    service._ensure_catalog = lambda: None
    service._view = ({}, {}, {"industry:a": {"600000.SH"}, "industry:b": {"600001.SH", "600000.SH"}})
    assert service.members_for(["industry:a", "industry:b", "unknown"]) == {"600000.SH", "600001.SH"}


def _sector_service(tmp_path, industries):
    from app.services.sector_monitor import SectorMonitorService

    service = SectorMonitorService(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    service._ensure_catalog = lambda: None
    targets = [{"key": key, "name": name, "kind": "industry", "level": level, "source_id": "ext_hy_ths",
                **({"available": False} if name.endswith("停用") else {})}
               for key, name, level, _members in industries]
    service._view = ({"industry": targets, "concept": []}, {t["key"]: t for t in targets},
                     {key: set(members) for key, _n, _l, members in industries})
    return service


def _members(n, include="688388.SH"):
    return [include, *[f"6000{i:02d}.SH" for i in range(n - 1)]]


def test_auto_industry_prefers_level_two(tmp_path):
    service = _sector_service(tmp_path, [("industry:l1", "电池", 1, _members(107)),
                                         ("industry:l2", "电池 / 锂电池", 2, _members(32))])
    assert service.auto_industry("688388.SH") == {"key": "industry:l2", "name": "电池 / 锂电池", "level": 2}


def test_auto_industry_falls_back_to_level_one_when_level_two_too_small(tmp_path):
    service = _sector_service(tmp_path, [("industry:l1", "电池", 1, _members(107)),
                                         ("industry:l2", "电池 / 小众", 2, _members(9))])
    assert service.auto_industry("688388.SH")["key"] == "industry:l1"


def test_auto_industry_skips_unavailable_and_unknown(tmp_path):
    service = _sector_service(tmp_path, [("industry:l2", "电池 / 停用", 2, _members(32))])
    assert service.auto_industry("688388.SH") is None
    assert service.auto_industry("000001.SZ") is None


def test_collect_auto_associates_industry_when_sector_unset(tmp_path, monkeypatch):
    adapter, pos, accounts, _fetch, now = _market_context(tmp_path, monkeypatch)
    pos = {**pos, "sector_id": ""}
    adapter.sectors.auto_industry = Mock(return_value={"key": "industry:auto", "name": "元件 / 印制电路板", "level": 2})
    seen = {}
    adapter.sectors.build_snapshots = lambda s, i, targets, w, *, now: seen.update(targets={t["key"] for t in targets}) or {
        "industry:auto": {"valid": True, "window_changes": {5: .002}, "coverage_ratio": 1}}
    env = adapter.collect([pos], accounts, now)[("a", "600000.SH")]["environment"]
    assert "industry:auto" in seen["targets"]
    assert (env["sector_id"], env["sector_basis"], env["sector_name"]) == ("industry:auto", "auto", "元件 / 印制电路板")
    assert env["sector_return_5m"] == .002


def test_collect_manual_sector_wins_over_auto(tmp_path, monkeypatch):
    adapter, pos, accounts, _fetch, now = _market_context(tmp_path, monkeypatch)
    adapter.sectors.auto_industry = Mock(return_value={"key": "industry:auto", "name": "x", "level": 2})
    env = adapter.collect([pos], accounts, now)[("a", "600000.SH")]["environment"]
    assert (env["sector_id"], env["sector_basis"]) == ("industry:test", "manual")
    adapter.sectors.auto_industry.assert_not_called()


def test_collect_without_any_sector_reports_none(tmp_path, monkeypatch):
    adapter, pos, accounts, _fetch, now = _market_context(tmp_path, monkeypatch)
    adapter.sectors.auto_industry = Mock(return_value=None)
    env = adapter.collect([{**pos, "sector_id": None}], accounts, now)[("a", "600000.SH")]["environment"]
    assert env["sector_id"] is None and env["sector_basis"] is None and env["sector_return_5m"] is None


def test_sector_time_uses_only_sector_members_not_whole_market(tmp_path, monkeypatch):
    """板块行情时间只看该板块参与计算的成分股; 全市场其他股票的旧报价不能拖累板块。"""
    adapter, pos, accounts, _fetch, now = _market_context(tmp_path, monkeypatch)
    cached = adapter.quotes.get_market_records()
    member_ms = (now - timedelta(seconds=2)).timestamp() * 1000
    other_ms = (now - timedelta(seconds=9)).timestamp() * 1000
    adapter.quotes.get_market_records = lambda: {
        **cached, "600000.SH": {**cached["600000.SH"], "timestamp": member_ms},
        "600009.SH": {**cached["600000.SH"], "symbol": "600009.SH", "timestamp": other_ms}}
    adapter.sectors.members_for = Mock(side_effect=lambda keys: {"600000.SH"} if keys == ["industry:test"] else set())
    env = adapter.collect([pos], accounts, now)[("a", "600000.SH")]["environment"]
    assert env["sector_ts"] == datetime.fromtimestamp(member_ms / 1000, CN_TZ).isoformat()


def test_sector_time_is_median_of_members_so_one_illiquid_stock_does_not_stale_it(tmp_path, monkeypatch):
    """成交清淡的成分股最近成交时间可能停在数十秒前 (实测最旧 30-45 秒, 中位 2-3 秒);
    板块行情时间取参与聚合成分股的中位市场时间, 代表板块整体新鲜度。"""
    adapter, pos, accounts, _fetch, now = _market_context(tmp_path, monkeypatch)
    cached = adapter.quotes.get_market_records()
    ages = {"600000.SH": 1, "600001.SH": 2, "600002.SH": 9}
    adapter.quotes.get_market_records = lambda: {
        **cached, **{s: {**cached["600000.SH"], "symbol": s, "timestamp": (now - timedelta(seconds=a)).timestamp() * 1000}
                     for s, a in ages.items()}}
    adapter.sectors.members_for = Mock(return_value=set(ages))
    env = adapter.collect([pos], accounts, now)[("a", "600000.SH")]["environment"]
    assert env["sector_ts"] == (now - timedelta(seconds=2)).isoformat()


def test_collect_attaches_regulatory_risk_flags(tmp_path, monkeypatch):
    from app.services import regulatory_reference

    adapter, pos, accounts, _fetch, now = _market_context(tmp_path, monkeypatch)
    calls = []

    def fake(day, data_dir):
        calls.append(day)
        return {"monitor": None, "anomaly": {"600000": {"rule": "30日偏离值-70%", "trigger_price": 9.5,
                                                        "required_pct": -0.05, "status": "reachable"}}}

    monkeypatch.setattr(regulatory_reference, "regulatory_reference", fake)
    env = adapter.collect([pos], accounts, now)[("a", "600000.SH")]["environment"]
    assert [f["id"] for f in env["risk_flags"]] == ["monitor_unknown", "anomaly_forecast"]
    adapter.collect([pos], accounts, now + timedelta(seconds=5))
    assert calls == [now.date()]  # 盘中按日缓存, 不每轮读盘


def test_collect_attaches_intraday_fund_evidence_without_touching_other_fields(tmp_path, monkeypatch):
    from app.services import fund_evidence

    adapter, pos, accounts, _fetch, now = _market_context(tmp_path, monkeypatch)
    calls = []

    def fake(symbols, at):
        calls.append((symbols, at))
        return {"600000.SH": {"status": "ok", "main_net": 1.5e8, "unit": "元", "available_at": at.isoformat()}}

    monkeypatch.setattr(fund_evidence, "intraday_main_net", fake)
    env = adapter.collect([pos], accounts, now)[("a", "600000.SH")]["environment"]
    assert calls == [(["600000.SH"], now)]
    assert env["fund_evidence"]["main_net"] == 1.5e8

    monkeypatch.setattr(fund_evidence, "intraday_main_net", lambda s, a: {})
    env2 = adapter.collect([pos], accounts, now + timedelta(seconds=5))[("a", "600000.SH")]["environment"]
    assert env2["fund_evidence"] is None
    assert {k: v for k, v in env2.items() if k != "fund_evidence"} ==         {k: v for k, v in env.items() if k != "fund_evidence"}
