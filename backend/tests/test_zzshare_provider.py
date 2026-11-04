from datetime import UTC, datetime
from itertools import pairwise

import httpx
import pytest

from app.data_providers.custom import loader
from app.plugins.zzshare import provider as zz

DAY = datetime(2026, 11, 2)


def bar(symbol="600000.SH", day="20261102", **changes):
    return dict({
        "ts_code": symbol, "trade_date": day, "open": 9.98, "high": 10.22, "low": 9.93,
        "close": 10.06, "volume": 116422276, "turnover": 1177122567.48, "factor": 13.37818,
    }, **changes)


def provider(monkeypatch, handler):
    monkeypatch.setattr(zz, "_INTERVAL", 0)
    return zz.ZzshareProvider(transport=httpx.MockTransport(handler))


def test_lookup_registration_and_asset_boundary(monkeypatch):
    monkeypatch.setattr(loader, "_PROVIDERS", {})
    monkeypatch.setattr(loader, "_PLUGIN_STATUS", {})
    monkeypatch.setattr(httpx.Client, "get", lambda *_a, **_k: pytest.fail("unexpected fetch"))
    loader._register_one_plugin(loader.plugin_manifest("zzshare"))
    p = loader.get_provider("zzshare")
    assert p.config.datasets.keys() == {"daily"}
    for asset in ("etf", "index"):
        assert p.get_daily(["000001.SH"], DAY, DAY, asset_type=asset).is_empty()
    assert "error" in p.test_dataset("adj_factor")
    p.close()


def test_range_normalizes_raw_prices_shares_yuan_and_local_day(monkeypatch):
    monkeypatch.delenv("ZZSHARE_TOKEN", raising=False)

    def handle(req):
        assert req.headers["sdk-key"] == "anonymous"
        assert req.url.params["candle_mode"] == "0"
        assert req.url.params["end_date"] == "20261102"
        return httpx.Response(200, json={"code": 200, "data": {
            "candle_mode": 0, "list": [bar(), bar(day="20261030", is_paused=1)],
        }})

    p = provider(monkeypatch, handle)
    df = p.get_daily(["600000.SH"], datetime(2026, 10, 30), datetime(2026, 11, 1, 18, tzinfo=UTC))
    assert df.to_dicts() == [{
        "symbol": "600000.SH", "date": DAY.date(), "open": 9.98, "high": 10.22, "low": 9.93,
        "close": 10.06, "volume": 1164222, "amount": 1177122567.48,
    }]


def test_large_pool_batches_market_pages_filters_and_reports_progress(monkeypatch):
    monkeypatch.setattr(zz, "_PAGE_SIZE", 2)
    pool = [f"600{i:03}.SH" for i in range(40)]
    calls = []

    def handle(req):
        assert req.url.path.endswith("/day")
        offset = int(req.url.params["offset"])
        calls.append(offset)
        rows = {0: [bar(), bar("000001.SZ")], 2: [bar("600001.SH")], 3: []}[offset]
        return httpx.Response(200, json={"code": 200, "data": {"list": rows, "count": len(rows)}})

    progress = []
    p = provider(monkeypatch, handle)
    df = p.get_daily(pool, DAY, DAY, on_chunk_done=lambda i, n: progress.append((i, n)))
    assert calls == [0, 2, 3]
    assert df["symbol"].to_list() == ["600000.SH", "600001.SH"]
    assert progress[-1] == (1, 1)


def test_wide_history_uses_nonoverlapping_short_date_windows(monkeypatch):
    windows = []

    def handle(req):
        params = req.url.params
        windows.append((params["start_date"], params["end_date"]))
        return httpx.Response(200, json={"code": 200, "data": {"list": []}})

    p = provider(monkeypatch, handle)
    assert p.get_daily(["600000.SH"], datetime(2024, 1, 1), DAY).is_empty()
    assert len(windows) >= 3
    assert windows[0][0] == "20240101" and windows[-1][1] == "20261102"
    for (_, end), (start, _) in pairwise(windows):
        assert (datetime.strptime(start, "%Y%m%d") - datetime.strptime(end, "%Y%m%d")).days == 1


def test_market_ignoring_offset_aborts_instead_of_completing_partial_sync(monkeypatch):
    p = provider(monkeypatch, lambda req: httpx.Response(200, json={
        "code": 200, "data": {"list": [bar()]},
    }))
    with pytest.raises(zz.ZzshareError, match="重复"):
        p.get_daily([f"600{i:03}.SH" for i in range(40)], DAY, DAY)


def test_preview_is_json_ready_and_requests_are_paced(monkeypatch):
    slept = []
    p = provider(monkeypatch, lambda req: httpx.Response(200, json={
        "code": 200, "data": {"list": [bar()]},
    }))
    monkeypatch.setattr(zz, "cn_now", lambda: DAY)
    monkeypatch.setattr(zz, "_INTERVAL", 2)
    monkeypatch.setattr(zz.time, "monotonic", lambda: 1)
    monkeypatch.setattr(zz.time, "sleep", slept.append)
    assert p.test_dataset("daily")["preview"][0]["date"] == "2026-11-02"
    assert p.test_dataset("daily")["rows"] == 1
    assert slept == [2]


@pytest.mark.parametrize("payload", [
    {"code": 401}, {"code": 200, "data": {}},
    {"code": 200, "data": {"list": [bar("000001.SZ")]}},
    {"code": 200, "data": {"list": [bar(day="20261103")]}},
    {"code": 200, "data": {"list": [bar(volume=None)]}},
    {"code": 200, "data": {"list": [bar(), bar()]}},
    {"code": 200, "data": {"candle_mode": 1, "list": [bar()]}},
])
def test_invalid_batch_fails_closed(monkeypatch, payload):
    p = provider(monkeypatch, lambda req: httpx.Response(200, json=payload))
    with pytest.raises(zz.ZzshareError):
        p.get_daily(["600000.SH"], DAY, DAY)


def test_limit_reasons_raw_rows_date_guard_and_rate_limit(monkeypatch):
    monkeypatch.setenv("ZZSHARE_TOKEN", "test-only")
    row = {"date": "2026-11-02", "stock_code": "600000", "stock_name": "浦发银行", "reason": "测试"}
    responses = [
        httpx.Response(200, json={"code": 20000, "data": [row, row]}),
        httpx.Response(200, json={"code": 20000, "data": [dict(row, date="2026-11-03")]}),
        httpx.Response(429),
    ]

    def handle(req):
        assert req.headers["sdk-key"] == "test-only"
        assert req.url.params["date1"] == "20261102"
        return responses.pop(0)

    p = provider(monkeypatch, handle)
    assert p.get_limit_reasons("2026-11-02") == [row, row]
    with pytest.raises(zz.ZzshareError):
        p.get_limit_reasons("2026-11-02")
    with pytest.raises(zz.ZzshareError, match="429"):
        p.get_limit_reasons("2026-11-02")
