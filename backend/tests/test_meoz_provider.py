"""MeozProvider (猫爪/猫头鹰) 契约与单位标准化测试。

不依赖真实网络与 API Key: 客户端层用 httpx.MockTransport, provider 层用假
MeozClient 按 apiname 返回 OpenAPI 文档形状的行, 验证信封解包、截断识别、
百分数→小数制、手/元口径、北京墙钟分钟与 241 根网格、累计因子→单事件比值、
请求切分不超过行数上限、失败语义 (软失败 / ProviderRequestError) 与能力声明。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import httpx
import polars as pl
import pytest

from app.data_providers.base import ProviderRequestError
from app.market_time import CN_TZ
from app.plugins.meoz import client as mc
from app.plugins.meoz import provider as mp
from app.plugins.meoz.provider import MeozProvider

# 2026-11-11 是周三; 盘中 10:05:20
_NOW = datetime(2026, 11, 11, 10, 5, 20, tzinfo=CN_TZ)
_TODAY = _NOW.date()


# ---------------------------------------------------------------- client 层


def _envelope(fields, items, code=200, message="success"):
    return {"code": code, "message": message, "data": {"fields": fields, "items": items}}


def _client(monkeypatch, handler, **kw) -> mc.MeozClient:
    monkeypatch.setattr(mc.time, "sleep", lambda _s: None)
    real = httpx.Client
    monkeypatch.setattr(
        mc.httpx, "Client", lambda **opts: real(transport=httpx.MockTransport(handler), **opts))
    return mc.MeozClient("k-test", min_interval_s=0, **kw)


def test_client_unpacks_fields_items_and_sends_documented_body(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        return httpx.Response(200, json=_envelope(["symbol", "vol"], [["000001.SZ", 12.0]]))

    rows = _client(monkeypatch, handler).query("minute", {"tradedate": "20261111"}, "symbol,vol")
    assert rows == [{"symbol": "000001.SZ", "vol": 12.0}]
    assert seen["url"] == mc.LIVE_URLS[0]
    assert '"apiname":"minute"' in seen["body"].replace(" ", "")
    assert '"apikey":"k-test"' in seen["body"].replace(" ", "")


def test_client_history_service_uses_history_node(monkeypatch):
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, json=_envelope(["a"], []))

    _client(monkeypatch, handler).query("minute", {"tradedate": "20261110"}, service="history")
    assert urls == [mc.HISTORY_URLS[0]]


def test_client_not_found_code_is_empty_success(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"code": 1002, "message": "未找到", "data": None})

    assert _client(monkeypatch, handler).query("daily") == []


@pytest.mark.parametrize("status,code", [(200, 401), (200, 403), (401, None)])
def test_client_auth_errors(monkeypatch, status, code):
    def handler(request):
        return httpx.Response(status, json={"code": code, "message": "Key 无效", "data": None})

    with pytest.raises(mc.MeozAuthError):
        _client(monkeypatch, handler).query("daily")


def test_client_business_error_raises(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"code": 1000, "message": "内部失败", "data": None})

    with pytest.raises(mc.MeozError, match="1000"):
        _client(monkeypatch, handler).query("daily")


def test_client_row_cap_is_treated_as_truncation(monkeypatch):
    items = [[str(i)] for i in range(mc.ROW_CAP)]

    def handler(request):
        return httpx.Response(200, json=_envelope(["symbol"], items))

    with pytest.raises(mc.MeozTruncatedError):
        _client(monkeypatch, handler).query("daily")


def test_client_retries_after_429(monkeypatch):
    responses = [httpx.Response(429, headers={"Retry-After": "2"}),
                 httpx.Response(200, json=_envelope(["a"], [[1]]))]

    def handler(request):
        return responses.pop(0)

    assert _client(monkeypatch, handler).query("daily") == [{"a": 1}]
    assert responses == []


def test_client_persistent_429_raises(monkeypatch):
    def handler(request):
        return httpx.Response(429)

    with pytest.raises(mc.MeozError, match="429"):
        _client(monkeypatch, handler).query("daily")


def test_client_fails_over_to_backup_node_on_transport_error(monkeypatch):
    urls = []

    def handler(request):
        urls.append(str(request.url))
        if str(request.url) == mc.LIVE_URLS[0]:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, json=_envelope(["a"], [[1]]))

    assert _client(monkeypatch, handler).query("daily") == [{"a": 1}]
    assert urls == list(mc.LIVE_URLS)


def test_client_requires_api_key():
    with pytest.raises(mc.MeozAuthError):
        mc.MeozClient("")


# ---------------------------------------------------------------- provider 层


class _FakeClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, dict, str | None, str]] = []

    def query(self, apiname, params=None, fields=None, *, service="live"):
        params = dict(params or {})
        self.calls.append((apiname, params, fields, service))
        return self.handler(apiname, params, fields, service)

    def close(self):
        pass


def _weekday_calendar(params):
    start = mp._parse_ymd(params["startdate"])
    end = mp._parse_ymd(params["enddate"])
    out, d = [], start
    while d <= end:
        out.append({"cal_date": mp._ymd(d), "is_open": "1" if d.weekday() < 5 else "0"})
        d += timedelta(days=1)
    return out


# stockbasic 实测: exchange 恒为 null, 全量单请求 (超限时按 market 分段)
_UNIVERSE = {
    "主板": [{"code": "600519.SH", "symbol": "600519"}, {"code": "000001.SZ", "symbol": "000001"}],
    "北交所": [{"code": "920001.BJ", "symbol": "920001"}],
}


def _provider(monkeypatch, handler, now: datetime = _NOW):
    def routed(apiname, params, fields, service):
        if apiname == "tradecal":
            return _weekday_calendar(params)
        if apiname == "stockbasic":
            assert "exchange" not in params
            if "market" in params:
                return _UNIVERSE.get(params["market"], [])
            return [row for rows in _UNIVERSE.values() for row in rows]
        return handler(apiname, params, fields, service)

    fake = _FakeClient(routed)
    monkeypatch.setattr(mp.meoz_client, "MeozClient", lambda **kw: fake)
    monkeypatch.setattr(mp, "get_api_key", lambda: "k-test")
    monkeypatch.setattr(mp, "cn_now", lambda: now)
    return MeozProvider(), fake


def _quote(symbol="600519", tradedate="20261111", **over):
    row = {"tradedate": tradedate, "symbol": symbol, "name": "贵州茅台", "open": 1460.0,
           "high": 1490.5, "low": 1455.0, "close": 1480.0, "pre_close": 1455.0,
           "change": 25.0, "pct_chg": 1.72, "vol": 32000.0, "amount": 4.7e9}
    row.update(over)
    return row


def test_realtime_units_suffix_and_server_side_full_market(monkeypatch):
    provider, fake = _provider(monkeypatch, lambda *a: [_quote(), _quote("000001", name="平安银行")])
    records = provider.get_realtime()
    by_symbol = {r["symbol"]: r for r in records}
    assert set(by_symbol) == {"600519.SH", "000001.SZ"}
    rec = by_symbol["600519.SH"]
    assert rec["change_pct"] == pytest.approx(0.0172)  # 百分数 → 小数制
    assert rec["volume"] == 32000.0  # 文档写明为手, 不换算
    assert rec["prev_close"] == 1455.0 and rec["last_price"] == 1480.0
    assert rec["turnover_rate"] is None and rec["amplitude"] is None
    daily_calls = [c for c in fake.calls if c[0] == "daily"]
    assert len(daily_calls) == 1 and "symbols" not in daily_calls[0][1]


def test_realtime_stale_trade_date_stamped_at_that_close(monkeypatch):
    provider, _ = _provider(monkeypatch, lambda *a: [_quote(tradedate="20261110")])
    rec = provider.get_realtime()[0]
    assert rec["timestamp"] == mp._ms(date(2026, 11, 10), mp._DAILY_CLOSE)


def test_realtime_derives_pct_when_missing(monkeypatch):
    provider, _ = _provider(monkeypatch, lambda *a: [_quote(pct_chg=None, change=None)])
    rec = provider.get_realtime()[0]
    assert rec["change_amount"] == pytest.approx(25.0)
    assert rec["change_pct"] == pytest.approx(25.0 / 1455.0)


def test_realtime_truncation_falls_back_to_universe_chunks(monkeypatch):
    def handler(apiname, params, fields, service):
        if "symbols" not in params:
            raise mc.MeozTruncatedError("cap")
        return [_quote(s) for s in params["symbols"].split(",")]

    provider, _ = _provider(monkeypatch, handler)
    records = provider.get_realtime()
    assert {r["symbol"] for r in records} == {"600519.SH", "000001.SZ", "920001.BJ"}


def test_realtime_unrecognized_rows_and_errors_return_empty(monkeypatch):
    provider, _ = _provider(monkeypatch, lambda *a: [{"tradedate": "20261111", "close": 1.0}])
    assert provider.get_realtime() == []

    def boom(*a):
        raise mc.MeozError("down")

    provider, _ = _provider(monkeypatch, boom)
    assert provider.get_realtime() == []


def test_realtime_symbols_etf_uses_full_code_and_failure_is_none(monkeypatch):
    def handler(apiname, params, fields, service):
        assert apiname == "etf_daily" and "code" in fields
        return [_quote("510300", code="510300.SH", name="沪深300ETF")]

    provider, _ = _provider(monkeypatch, handler)
    rows = provider.get_realtime_symbols(["510300.SH"], asset_type="etf")
    assert [r["symbol"] for r in rows] == ["510300.SH"]

    def boom(*a):
        raise mc.MeozError("down")

    provider, _ = _provider(monkeypatch, boom)
    assert provider.get_realtime_symbols(["510300.SH"], asset_type="etf") is None


def test_realtime_indices_drop_unconfirmed_volume_and_use_servertime(monkeypatch):
    rows = [
        {"tradedate": "20261111", "symbol": "000300.SH", "name": "沪深300", "close": 4042.18,
         "pre_close": 4005.26, "pct_chg": 0.92, "amount": 2.8e11, "servertime": "10:05:15"},
        {"tradedate": "20261111", "symbol": "000300.SZ", "close": 1.0},
    ]
    provider, fake = _provider(monkeypatch, lambda *a: rows)
    out = provider.get_realtime_indices(["000300.SH"])
    assert [r["symbol"] for r in out] == ["000300.SH"]
    assert out[0]["volume"] is None
    assert out[0]["change_pct"] == pytest.approx(0.0092)
    expected = int(datetime(2026, 11, 11, 10, 5, 15, tzinfo=CN_TZ).timestamp() * 1000)
    assert out[0]["timestamp"] == expected
    assert fake.calls[-1][1]["symbols"] == "000300"

    def boom(*a):
        raise mc.MeozError("down")

    provider, _ = _provider(monkeypatch, boom)
    assert provider.get_realtime_indices(["000300.SH"]) is None


def test_daily_maps_raw_rows_and_respects_row_budget(monkeypatch):
    symbols = [f"{600000 + i}.SH" for i in range(1200)]

    def handler(apiname, params, fields, service):
        assert apiname == "daily"
        return [_quote(params["symbols"].split(",")[0], tradedate="20261109")]

    provider, fake = _provider(monkeypatch, handler)
    start = datetime(2026, 10, 12, tzinfo=CN_TZ)
    frame = provider.get_daily(symbols, start, _NOW)
    daily_calls = [c for c in fake.calls if c[0] == "daily"]
    trading_days = len(provider._trading_days(start.date(), _TODAY))
    for _, params, _, _ in daily_calls:
        assert len(params["symbols"].split(",")) * trading_days <= mp._ROW_BUDGET
    assert sum(len(c[1]["symbols"].split(",")) for c in daily_calls) == 1200
    row = frame.filter(pl.col("symbol") == "600000.SH").to_dicts()[0]
    assert row["date"] == date(2026, 11, 9)
    assert row["close"] == 1480.0 and row["volume"] == 32000.0 and row["amount"] == 4.7e9


def test_daily_drops_intraday_partial_bar_before_close(monkeypatch):
    rows = [_quote(tradedate="20261110"), _quote(tradedate="20261111")]
    provider, _ = _provider(monkeypatch, lambda *a: rows)
    frame = provider.get_daily(["600519.SH"], datetime(2026, 11, 9), _NOW)
    assert frame["date"].to_list() == [date(2026, 11, 10)]

    after_close = _NOW.replace(hour=15, minute=30)
    provider, _ = _provider(monkeypatch, lambda *a: rows, now=after_close)
    frame = provider.get_daily(["600519.SH"], datetime(2026, 11, 9), after_close)
    assert frame["date"].to_list() == [date(2026, 11, 10), date(2026, 11, 11)]


def test_daily_index_not_provided_and_progress_closed(monkeypatch):
    provider, _ = _provider(monkeypatch, lambda *a: pytest.fail("不应请求"))
    progress = []
    frames = list(provider.iter_daily(["000300.SH"], datetime(2026, 11, 1), _NOW, "index",
                                      on_chunk_done=lambda c, t: progress.append((c, t))))
    assert frames == [] and progress == [(0, 0)]


def test_daily_request_failure_raises_provider_error(monkeypatch):
    def boom(*a):
        raise mc.MeozError("down")

    provider, _ = _provider(monkeypatch, boom)
    with pytest.raises(ProviderRequestError):
        provider.get_daily(["600519.SH"], datetime(2026, 11, 1), _NOW)


def _factor_handler(series: dict[str, dict[str, float]]):
    def handler(apiname, params, fields, service):
        assert apiname == "adj_factor"
        out = []
        for six in params["symbols"].split(","):
            code = next(c for c in series if c.startswith(six))
            for day, factor in series[code].items():
                if "tradedate" in params:
                    keep = day == params["tradedate"]
                else:
                    keep = params["startdate"] <= day <= params["enddate"]
                if keep:
                    out.append({"tradedate": day, "symbol": six, "code": code, "adj_factor": factor})
        return out

    return handler


def _days(start: date, end: date) -> list[str]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(mp._ymd(d))
        d += timedelta(days=1)
    return out


def test_adj_factor_ratio_becomes_single_event_ex_factor(monkeypatch):
    days = _days(date(2026, 9, 1), date(2026, 11, 11))
    flat = dict.fromkeys(days, 10.0)
    stepped = {d: (10.0 if d < "20261016" else 10.046) for d in days}
    provider, _ = _provider(monkeypatch, _factor_handler(
        {"000655.SZ": stepped, "600519.SH": flat}))
    frame = provider.get_adj_factors(["000655.SZ", "600519.SH"],
                                     datetime(2026, 10, 1), datetime(2026, 11, 11))
    assert frame.to_dicts() == [
        {"symbol": "000655.SZ", "trade_date": date(2026, 10, 16), "ex_factor": pytest.approx(1.0046)},
    ]


def test_adj_factor_prefilter_fetches_series_only_for_changed(monkeypatch):
    days = _days(date(2026, 9, 1), date(2026, 11, 11))
    series = {f"{600000 + i}.SH": dict.fromkeys(days, 5.0) for i in range(60)}
    series["600007.SH"] = {d: (5.0 if d < "20261102" else 5.5) for d in days}
    provider, fake = _provider(monkeypatch, _factor_handler(series))
    frame = provider.get_adj_factors(list(series), datetime(2026, 10, 28), datetime(2026, 11, 11))
    assert frame["symbol"].to_list() == ["600007.SH"]
    assert frame["ex_factor"].to_list() == [pytest.approx(1.1)]
    ranged = [c for c in fake.calls if c[0] == "adj_factor" and "startdate" in c[1]]
    assert [c[1]["symbols"] for c in ranged] == ["600007"]


def test_adj_factor_decreasing_series_fails_closed(monkeypatch):
    days = _days(date(2026, 9, 1), date(2026, 11, 11))
    inverted = {d: (1.0 if d < "20261016" else 0.99) for d in days}
    provider, _ = _provider(monkeypatch, _factor_handler({"000655.SZ": inverted}))
    with pytest.raises(ProviderRequestError, match="下降"):
        provider.get_adj_factors(["000655.SZ"], datetime(2026, 10, 1), datetime(2026, 11, 11))


def test_adj_factor_etf_returns_empty_without_requests(monkeypatch):
    provider, fake = _provider(monkeypatch, lambda *a: pytest.fail("不应请求"))
    assert provider.get_adj_factors(["510300.SH"], None, None, asset_type="etf").is_empty()
    assert fake.calls == []


def _minute_rows(code: str, tradedate: str, labels: list[str]):
    return [{"tradedate": tradedate, "symbol": code, "trademin": lab,
             "time": f"{lab[:2]}:{lab[2:]}:00", "open": 10.0, "high": 10.5, "low": 9.9,
             "close": 10.2, "vol": 120.0, "amount": 122_400.0} for lab in labels]


def test_minute_history_and_live_service_split_with_grid(monkeypatch):
    def handler(apiname, params, fields, service):
        assert apiname == "minute"
        rows = []
        for six in params["symbols"].split(","):
            code = {"600519": "600519.SH", "000001": "000001.SZ"}[six]
            rows += _minute_rows(code, params["tradedate"], ["0930", "0931", "1500"])
        return rows

    provider, fake = _provider(monkeypatch, handler)
    frame = provider.get_minute(["600519.SH", "000001.SZ"],
                                datetime(2026, 11, 10, 9, 25, tzinfo=CN_TZ), _NOW)
    services = {c[1]["tradedate"]: c[3] for c in fake.calls if c[0] == "minute"}
    assert services == {"20261110": "history", "20261111": "live"}
    assert frame.schema["datetime"] == pl.Datetime("us")
    first = frame.filter(pl.col("symbol") == "600519.SH").sort("datetime").to_dicts()[0]
    assert first["datetime"] == datetime(2026, 11, 10, 9, 30)
    assert first["volume"] == 120.0 and first["amount"] == 122_400.0
    assert set(frame["minute_time_grid"].to_list()) == {"auction_end"}
    today = frame.filter(pl.col("datetime").dt.date() == _TODAY)
    assert set(today["minute_price_basis"].to_list()) == {"raw"}
    history = frame.filter(pl.col("datetime").dt.date() == date(2026, 11, 10))
    assert history["minute_price_basis"].null_count() == history.height
    # 窗口截止 10:05:20, 今日 15:00 行不应出现
    assert today["datetime"].max() <= datetime(2026, 11, 11, 10, 5, 20)


def test_live_minute_uses_bucket_label_not_last_tick_time(monkeypatch):
    # 实测 (2026-11-12): 日内服务 time 为桶内最后一笔时刻, trademin 才是收盘桶
    rows = [
        {"tradedate": "20261111", "symbol": "600519.SH", "trademin": "0930", "time": "09:25:01",
         "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "vol": 10.0, "amount": 1000.0},
        {"tradedate": "20261111", "symbol": "600519.SH", "trademin": "1006", "time": "10:05:59",
         "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "vol": 10.0, "amount": 1000.0},
    ]
    provider, _ = _provider(monkeypatch, lambda *a: rows)
    frame = provider.get_intraday_batch(["600519.SH"])
    assert frame["datetime"].to_list() == [datetime(2026, 11, 11, 9, 30),
                                           datetime(2026, 11, 11, 10, 6)]


def test_minute_chunks_24_symbols_per_request(monkeypatch):
    symbols = [f"{600000 + i}.SH" for i in range(50)]
    provider, fake = _provider(monkeypatch, lambda *a: [])
    progress = []
    list(provider.iter_minute(symbols, datetime(2026, 11, 11, 9, 25), _NOW,
                              on_chunk_done=lambda c, t: progress.append((c, t))))
    sizes = [len(c[1]["symbols"].split(",")) for c in fake.calls if c[0] == "minute"]
    assert sizes == [24, 24, 2]
    assert progress[-1] == (3, 3)


def test_minute_unsupported_inputs_and_failure(monkeypatch):
    provider, _ = _provider(monkeypatch, lambda *a: pytest.fail("不应请求"))
    for kwargs in ({"freq": "5m"}, {"asset_type": "index"}):
        progress: list[tuple[int, int]] = []
        frames = list(provider.iter_minute(
            ["600519.SH"], None, None,
            on_chunk_done=lambda c, t, sink=progress: sink.append((c, t)), **kwargs))
        assert frames == []
        assert progress == [(0, 0)]

    def boom(*a):
        raise mc.MeozError("down")

    provider, _ = _provider(monkeypatch, boom)
    with pytest.raises(ProviderRequestError):
        provider.get_minute(["600519.SH"], None, _NOW)


def test_intraday_latest_one_full_market_request_per_bucket(monkeypatch):
    def handler(apiname, params, fields, service):
        assert "symbols" not in params and service == "live"
        return (_minute_rows("600519.SH", params["tradedate"], [params["trademin"]])
                + _minute_rows("000001.SZ", params["tradedate"], [params["trademin"]]))

    provider, fake = _provider(monkeypatch, handler)
    frame = provider.get_intraday_latest(count=3)
    assert [c[1]["trademin"] for c in fake.calls] == ["1004", "1005", "1006"]
    assert frame.height == 6
    assert set(frame["minute_price_basis"].to_list()) == {"raw"}
    only = provider.get_intraday_latest(symbols=["000001.SZ"], count=1)
    assert only["symbol"].to_list() == ["000001.SZ"]


def test_full_minute_frames_pass_service_wallclock_guard(monkeypatch):
    from app.services import kline_sync

    def handler(apiname, params, fields, service):
        labels = [params["trademin"]] if "trademin" in params else ["0930", "0931", "1005"]
        return _minute_rows("600519.SH", params["tradedate"], labels)

    provider, _ = _provider(monkeypatch, handler)
    latest, requests = kline_sync.fetch_intraday_custom_latest(provider, "meoz", count=2)
    assert requests == 1 and latest.height == 2
    assert latest["datetime"].to_list() == [datetime(2026, 11, 11, 10, 5),
                                            datetime(2026, 11, 11, 10, 6)]
    assert set(latest["minute_time_grid"].to_list()) == {"auction_end"}
    batch, _ = kline_sync.fetch_intraday_custom_batch(provider, "meoz", ["600519.SH"])
    assert batch["datetime"].min() == datetime(2026, 11, 11, 9, 30)
    assert set(batch["minute_price_basis"].to_list()) == {"raw"}


def test_intraday_latest_truncation_requeries_by_universe(monkeypatch):
    def handler(apiname, params, fields, service):
        if "symbols" not in params:
            raise mc.MeozTruncatedError("cap")
        return _minute_rows("920001.BJ", params["tradedate"], [params["trademin"]])

    provider, _ = _provider(monkeypatch, handler)
    assert provider.get_intraday_latest(count=1)["symbol"].to_list() == ["920001.BJ"]


def test_intraday_latest_before_auction_is_empty(monkeypatch):
    early = _NOW.replace(hour=9, minute=10)
    provider, fake = _provider(monkeypatch, lambda *a: pytest.fail("不应请求"), now=early)
    assert provider.get_intraday_latest().is_empty() and fake.calls == []


def test_intraday_batch_skips_failed_group_and_stops_on_auth(monkeypatch):
    calls = {"n": 0}

    def handler(apiname, params, fields, service):
        calls["n"] += 1
        if calls["n"] == 1:
            raise mc.MeozError("transient")
        if calls["n"] == 3:
            raise mc.MeozAuthError("quota")
        six = params["symbols"].split(",")[0]
        return _minute_rows(f"{six}.SH", params["tradedate"], ["0930"])

    symbols = [f"{600000 + i}.SH" for i in range(80)]
    provider, _ = _provider(monkeypatch, handler)
    frame = provider.get_intraday_batch(symbols)
    assert calls["n"] == 3  # 第 4 组因凭据被拒中止
    assert frame["symbol"].to_list() == ["600024.SH"]


def test_datasets_declaration_and_unsupported_test_dataset(monkeypatch):
    provider, _ = _provider(monkeypatch, lambda *a: [])
    assert set(provider.config.datasets) == {"realtime", "daily", "adj_factor", "minute",
                                             "full_minute"}
    assert "depth5" not in provider.config.datasets
    assert "financial" not in provider.config.datasets
    out = provider.test_dataset("depth5")
    assert out["rows"] == 0 and "未接入" in out["error"]


def test_test_dataset_reports_errors_instead_of_raising(monkeypatch):
    def boom(*a):
        raise mc.MeozError("Key 无效")

    provider, _ = _provider(monkeypatch, boom)
    out = provider.test_dataset("daily", ["600519.SH"])
    assert out["rows"] == 0 and "Key 无效" in out["error"]


def test_get_api_key_secrets_priority_and_availability(monkeypatch):
    from app import secrets_store

    monkeypatch.setenv(mp.API_KEY_ENV, "from-env")
    monkeypatch.setattr(secrets_store, "load", lambda: {mp.SECRETS_FIELD: "from-ui"})
    assert mp.get_api_key() == "from-ui"
    monkeypatch.setattr(secrets_store, "load", lambda: {})
    assert mp.get_api_key() == "from-env"
    assert mp.availability() == (True, "ok")
    monkeypatch.delenv(mp.API_KEY_ENV)
    ok, reason = mp.availability()
    assert not ok and mp.API_KEY_ENV in reason


def test_probe_api_key_semantics(monkeypatch):
    class _Probe:
        def __init__(self, error=None):
            self.error = error

        def query(self, apiname, params=None, fields=None, *, service="live"):
            assert apiname == "tradecal"
            if self.error:
                raise self.error
            return [{"cal_date": "20261111", "is_open": "1"}]

        def close(self):
            pass

    monkeypatch.setattr(mp.meoz_client, "MeozClient", lambda **kw: _Probe())
    assert mp.probe_api_key("good") == (True, "ok")
    monkeypatch.setattr(mp.meoz_client, "MeozClient",
                        lambda **kw: _Probe(mc.MeozAuthError("401")))
    ok, reason = mp.probe_api_key("bad")
    assert not ok and "无效" in reason


def test_loader_manifest_declares_plugin():
    from app.data_providers.custom import loader

    manifest = loader.plugin_manifest("meoz")
    assert manifest is not None
    datasets = manifest["datasets"] if isinstance(manifest, dict) else manifest.datasets
    assert set(datasets) == {"realtime", "daily", "adj_factor", "minute", "full_minute"}
