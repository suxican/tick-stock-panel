"""Domestic-reachable overseas quotes: fixed vendor samples, no network."""
from datetime import UTC, date, datetime

import httpx
import pytest
import yaml

from app.data_providers.custom import loader
from app.plugins.cn_overseas import provider as cn

# 2026-11-18 14:00 UTC = 09:00 New York (before the open), 23:00 Tokyo/Seoul.
FETCHED = datetime(2026, 11, 18, 14, tzinfo=UTC)

_TENCENT_FIELDS = {
    "usIXIC": (".IXIC", "24328.38", "24509.76", "2026-11-17 17:15:59", "-0.74"),
    "usDJI": (".DJI", "50759.42", "51608.17", "2026-11-17 16:42:18", "-1.64"),
    "usINX": (".INX", "7340.53", "7416.59", "2026-11-17 16:42:12", "-1.03"),
}
_SINA_FIELDS = {
    "znb_NKY": "日经225,59024.0600,163.63,0.28,2:12 AM,1759126320,2026-11-18,14:30:01,"
               "59007.4000,58860.4300,59630.9500,58279.2600,0",
    "znb_KOSPI": "首尔综合指数,5067.4400,98.96,1.99,2:27 AM,1759127220,2026-11-18,14:30:40,"
                 "5098.8200,4968.4800,5133.1400,4943.1700,0",
}


def _tencent_line(code, overrides=None):
    vendor_code, price, prev, at, pct = _TENCENT_FIELDS[code]
    fields = ["0"] * 50
    fields[0], fields[1], fields[2] = "200", "名称", vendor_code
    fields[3], fields[4], fields[30], fields[32], fields[35] = price, prev, at, pct, "USD"
    for index, value in (overrides or {}).items():
        fields[index] = value
    return f'v_{code}="{"~".join(fields)}";\n'


def _quote_handler(tencent=None, sina=None, calls=None):
    tencent = tencent or {}
    sina = sina or {}

    def handler(request):
        if calls is not None:
            calls.append(request.url.host)
        if request.url.host == "qt.gtimg.cn":
            if "status" in tencent:
                return httpx.Response(tencent["status"])
            codes = request.url.path.removeprefix("/q=").split(",")
            body = "".join(_tencent_line(code, tencent.get(code)) for code in codes)
            return httpx.Response(200, content=body.encode("gbk"))
        if request.url.host == "hq.sinajs.cn":
            assert request.headers["Referer"] == "https://finance.sina.com.cn/"
            if "status" in sina:
                return httpx.Response(sina["status"])
            codes = request.url.path.removeprefix("/list=").split(",")
            body = "".join(f'var hq_str_{code}="{sina.get(code, _SINA_FIELDS[code])}";\n' for code in codes)
            return httpx.Response(200, content=body.encode("gb18030"))
        raise AssertionError(f"unexpected host {request.url.host}")
    return handler


def _provider(handler, now=FETCHED):
    return cn.CnOverseasProvider(
        clock=lambda: now,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_market_game_quotes_keep_four_rows_with_decimal_changes_and_local_dates():
    calls = []
    result = _provider(_quote_handler(calls=calls)).get_overseas_quotes()
    assert result["source"] == "cn_overseas"
    assert result["fetched_at"] == FETCHED.isoformat()
    rows = {row["symbol"]: row for row in result["quotes"]}
    assert list(rows) == ["^IXIC", "^KS11", "005930.KS", "000660.KS"]
    nasdaq = rows["^IXIC"]
    assert nasdaq["state"] == "ready"
    assert nasdaq["price"] == 24328.38 and nasdaq["previous_close"] == 24509.76
    assert nasdaq["change_pct"] == pytest.approx(24328.38 / 24509.76 - 1)
    assert nasdaq["session_date"] == "2026-11-17"
    assert nasdaq["phase"] == "closed"
    assert nasdaq["observed_at"] == "2026-11-17T22:15:59+00:00"
    kospi = rows["^KS11"]
    assert kospi["state"] == "ready"
    assert kospi["change_pct"] == pytest.approx(5067.44 / 4968.48 - 1)
    assert kospi["session_date"] == "2026-11-18"
    assert kospi["phase"] == "closed"
    for symbol in ("005930.KS", "000660.KS"):
        assert rows[symbol]["state"] == "unavailable"
        assert rows[symbol]["price"] is None and rows[symbol]["change_pct"] is None
        assert "韩国个股" in rows[symbol]["reason"]
    # One batched request per vendor; Korean equities are never requested.
    assert sorted(calls) == ["hq.sinajs.cn", "qt.gtimg.cn"]


def test_index_quotes_isolate_a_failed_vendor():
    rows = _provider(_quote_handler(tencent={"status": 502})).get_overseas_index_quotes()
    by_symbol = {row["symbol"]: row for row in rows}
    assert set(by_symbol) == {"^DJI", "^GSPC", "^IXIC", "^N225", "^KS11"}
    for symbol in ("^DJI", "^GSPC", "^IXIC"):
        assert by_symbol[symbol]["state"] == "unavailable"
        assert by_symbol[symbol]["change_pct"] is None
    assert by_symbol["^N225"]["state"] == "ready"
    assert by_symbol["^N225"]["session_date"] == "2026-11-18"
    assert by_symbol["^N225"]["currency"] == "JPY"


def test_connection_failure_is_explicit_without_retry():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        raise httpx.ConnectTimeout("timeout", request=request)

    rows = _provider(handler).get_overseas_index_quotes()
    assert all(row["state"] == "unavailable" and row["price"] is None for row in rows)
    assert sorted(calls) == ["hq.sinajs.cn", "qt.gtimg.cn"]


@pytest.mark.parametrize("overrides,reason", [
    ({2: ".NDX"}, "标的"),          # Nasdaq-100 must not stand in for the composite.
    ({35: "CNY"}, "标的"),
    ({32: "-0.95"}, "涨跌幅"),       # Vendor change disagrees with price/previous close.
    ({30: "2026-11-18 09:30:00"}, "晚于"),  # Later than the actual fetch time.
    ({3: "0"}, "核验"),
])
def test_unverifiable_us_quote_never_yields_a_return(overrides, reason):
    rows = _provider(_quote_handler(tencent={"usIXIC": overrides})).get_overseas_index_quotes()
    row = next(row for row in rows if row["symbol"] == "^IXIC")
    assert row["state"] == "unverified"
    assert row["change_pct"] is None
    assert reason in row["reason"]
    assert next(row for row in rows if row["symbol"] == "^DJI")["state"] == "ready"


@pytest.mark.parametrize("body,state", [
    ("", "unavailable"),
    ("日经平均,59024.06,163.63,0.28,2:12 AM,1,2026-11-18,14:30:01,1,58860.43,1,1,0", "unverified"),
    ("日经225,59024.06,163.63,0.28,2:12 AM,1,2026-11-18,14:30:01,1,50000,1,1,0", "unverified"),
])
def test_empty_or_mismatched_sina_quote_is_not_ready(body, state):
    rows = _provider(_quote_handler(sina={"znb_NKY": body})).get_overseas_index_quotes()
    row = next(row for row in rows if row["symbol"] == "^N225")
    assert row["state"] == state
    assert row["change_pct"] is None


def test_quote_during_the_session_is_intraday():
    # 2026-11-18 02:00 UTC = 11:00 Seoul.
    now = datetime(2026, 11, 18, 2, tzinfo=UTC)
    body = _SINA_FIELDS["znb_KOSPI"].replace("14:30:40", "09:59:30")
    rows = _provider(_quote_handler(sina={"znb_KOSPI": body}), now=now).get_overseas_index_quotes()
    row = next(row for row in rows if row["symbol"] == "^KS11")
    assert row["state"] == "ready"
    assert row["phase"] == "intraday"


def _kline_handler(rows, calls=None):
    def handler(request):
        if calls is not None:
            calls.append(request.url)
        if request.url.host == "web.ifzq.gtimg.cn":
            code = request.url.params["param"].split(",")[0]
            return httpx.Response(200, json={"code": 0, "data": {code: {"day": rows}}})
        if request.url.host == "gi.finance.sina.com.cn":
            return httpx.Response(200, json={"result": {"status": {"code": 0}, "data": rows}})
        raise AssertionError(f"unexpected host {request.url.host}")
    return handler


def test_us_daily_is_inclusive_and_drops_unfinished_session():
    calls = []
    vendor = [
        ["2026-11-13", "7616.85", "7561.67", "7621.78", "7499.40", "2727332784.000"],
        ["2026-11-16", "7513.98", "7416.59", "7515.60", "7400.15", "3068678237.000"],
        ["2026-11-17", "7411.02", "7340.53", "7415.37", "7288.34", "3185916824.000"],
        ["2026-11-18", "7341.00", "7350.00", "7360.00", "7330.00", "1000.000"],
    ]
    # 2026-11-18 18:00 UTC = 13:00 New York, so the 11-18 bar is still forming.
    now = datetime(2026, 11, 18, 18, tzinfo=UTC)
    rows = _provider(_kline_handler(vendor, calls), now=now).get_overseas_index_daily(
        "^GSPC", date(2026, 11, 16), date(2026, 11, 18))
    assert rows == [
        {"symbol": "^GSPC", "date": "2026-11-16", "open": 7513.98, "high": 7515.60,
         "low": 7400.15, "close": 7416.59, "volume": 3068678237.0},
        {"symbol": "^GSPC", "date": "2026-11-17", "open": 7411.02, "high": 7415.37,
         "low": 7288.34, "close": 7340.53, "volume": 3185916824.0},
    ]
    assert len(calls) == 1
    assert calls[0].params["param"] == "us.INX,day,2026-11-16,2026-11-18,3,qfq"


def test_japan_daily_keeps_closed_session_and_blank_volume():
    vendor = [
        {"d": "2026-11-17", "v": "138500000", "c": "58860.4297", "o": "59295.8008",
         "l": "58691.1094", "h": "59369.1406"},
        {"d": "2026-11-18", "v": "0", "c": "59024.2813", "o": "59007.3984",
         "l": "58279.2617", "h": "59630.9492"},
    ]
    rows = _provider(_kline_handler(vendor)).get_overseas_index_daily(
        "^N225", date(2026, 11, 18), date(2026, 11, 18))
    assert rows == [{"symbol": "^N225", "date": "2026-11-18", "open": 59007.3984,
                     "high": 59630.9492, "low": 58279.2617, "close": 59024.2813, "volume": None}]


def test_daily_rejects_unsupported_symbols_without_request():
    calls = []
    provider = _provider(_kline_handler([], calls))
    with pytest.raises(ValueError):
        provider.get_overseas_index_daily("005930.KS", date(2026, 11, 1), date(2026, 11, 18))
    assert calls == []


def test_daily_invalid_ohlc_raises_instead_of_returning_wrong_bars():
    vendor = [{"d": "2026-11-17", "v": "1", "c": "abc", "o": "1", "l": "1", "h": "1"}]
    with pytest.raises(ValueError):
        _provider(_kline_handler(vendor)).get_overseas_index_daily(
            "^KS11", date(2026, 11, 17), date(2026, 11, 17))


def test_plugin_registration_does_not_fetch(monkeypatch):
    monkeypatch.setattr(loader, "_PROVIDERS", {})
    monkeypatch.setattr(loader, "_PLUGIN_STATUS", {})
    monkeypatch.setattr(httpx.Client, "get", lambda *_a, **_k: pytest.fail("registration used network"))
    manifest = yaml.safe_load((loader.plugins_dir() / "cn_overseas" / "plugin.yaml").read_text(encoding="utf-8"))
    loader._register_one_plugin(manifest)
    assert loader.provider_has_dataset("cn_overseas", "overseas")
    assert loader._PLUGIN_STATUS["cn_overseas"]["available"] is True
    assert loader._PLUGIN_STATUS["cn_overseas"]["runtime"] == "none"
