"""Fixed quote fixtures verify units, dates and unavailable paths without network."""
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import httpx
import pytest
import yaml

from app.data_providers.custom import loader
from app.plugins.yahoo_overseas import provider as yahoo

FETCHED = datetime(2026, 10, 20, 21, tzinfo=UTC)


def _payload(spec):
    tz = ZoneInfo(spec.timezone)
    start = datetime(2026, 10, 20, 9, 30 if spec.market == "US" else 0, tzinfo=tz)
    end = datetime(2026, 10, 20, 16 if spec.market == "US" else 15,
                   0 if spec.market == "US" else 30, tzinfo=tz)
    return {"chart": {"error": None, "result": [{
        "meta": {
            "symbol": spec.symbol, "instrumentType": spec.kind, "currency": spec.currency,
            "exchangeTimezoneName": spec.timezone, "regularMarketTime": int(end.timestamp()),
            "regularMarketPrice": 110, "chartPreviousClose": 1,
            "currentTradingPeriod": {"regular": {"start": int(start.timestamp()), "end": int(end.timestamp())}},
        },
        "timestamp": [int((start - timedelta(days=1)).timestamp()), int(start.timestamp())],
        "indicators": {
            "quote": [{"open": [99, 101], "high": [102, 112], "low": [98, 100], "close": [100, 110]}],
            "adjclose": [{"adjclose": [100, 110]}],
        },
        "events": {},
    }]}}


def _provider(handler):
    return yahoo.YahooOverseasProvider(
        clock=lambda: FETCHED,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_acquisition_normalizes_all_four_symbols_and_decimal_changes():
    def handler(request):
        spec = next(spec for spec in yahoo._INSTRUMENTS if unquote(request.url.path).endswith(spec.symbol))
        return httpx.Response(200, json=_payload(spec))

    result = _provider(handler).get_overseas_quotes()
    assert result["source"] == "yahoo_overseas"
    assert len(result["quotes"]) == 4
    for row in result["quotes"]:
        assert row["state"] == "ready"
        assert row["previous_close"] == 100
        assert row["change_pct"] == pytest.approx(0.1)
        assert row["session_date"] == "2026-10-20"
        assert row["phase"] == "closed"
        assert row["available_at"] == result["fetched_at"] == FETCHED.isoformat()


@pytest.mark.parametrize("status", [429, 500, 302])
def test_request_failure_is_explicit_without_retry_or_fallback(status):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(status)

    rows = _provider(handler).get_overseas_quotes()["quotes"]
    assert len(calls) == 4
    assert all(row["state"] == "unavailable" and row["change_pct"] is None for row in rows)


def test_one_timeout_does_not_drop_other_symbols():
    def handler(request):
        spec = next(spec for spec in yahoo._INSTRUMENTS if unquote(request.url.path).endswith(spec.symbol))
        if spec.symbol == "^KS11":
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(200, json=_payload(spec))

    rows = _provider(handler).get_overseas_quotes()["quotes"]
    assert [row["state"] for row in rows] == ["ready", "unavailable", "ready", "ready"]


@pytest.mark.parametrize("field,value", [
    ("currency", "CNY"), ("symbol", "000001.SZ"), ("instrumentType", "ETF"),
    ("exchangeTimezoneName", "Asia/Shanghai"), ("regularMarketPrice", float("nan")),
    ("regularMarketTime", int((FETCHED + timedelta(seconds=1)).timestamp())),
])
def test_invalid_identity_price_or_future_quote_never_yields_a_return(field, value):
    spec = yahoo._INSTRUMENTS[0]
    payload = _payload(spec)
    payload["chart"]["result"][0]["meta"][field] = value
    row = yahoo._normalize(payload, spec, FETCHED)
    assert row["state"] == "unverified"
    assert row["change_pct"] is None


def test_equity_ex_dividend_window_is_not_reported_as_market_return():
    spec = yahoo._INSTRUMENTS[2]
    payload = _payload(spec)
    result = payload["chart"]["result"][0]
    result["events"] = {"dividends": {"event": {"date": result["timestamp"][-1]}}}
    row = yahoo._normalize(payload, spec, FETCHED)
    assert row["state"] == "unverified"
    assert row["change_pct"] is None
    assert "除权" in row["reason"]


def test_missing_previous_close_does_not_use_chart_range_baseline():
    spec = yahoo._INSTRUMENTS[0]
    payload = _payload(spec)
    payload["chart"]["result"][0]["indicators"]["quote"][0]["close"][-2] = None
    row = yahoo._normalize(payload, spec, FETCHED)
    assert row["state"] == "unverified"
    assert row["change_pct"] is None


def test_plugin_registration_does_not_fetch_quotes(monkeypatch):
    monkeypatch.setattr(loader, "_PROVIDERS", {})
    monkeypatch.setattr(loader, "_PLUGIN_STATUS", {})
    monkeypatch.setattr(httpx.Client, "get", lambda *_args, **_kwargs: pytest.fail("registration used network"))
    manifest = yaml.safe_load((loader.plugins_dir() / "yahoo_overseas" / "plugin.yaml").read_text(encoding="utf-8"))
    loader._register_one_plugin(manifest)
    assert loader.provider_has_dataset("yahoo_overseas", "overseas")
    assert loader._PLUGIN_STATUS["yahoo_overseas"]["available"] is True
    # Only core dependencies are used; there is no installable requirements file.
    assert loader._PLUGIN_STATUS["yahoo_overseas"]["runtime"] == "none"
