"""全A指数取数与已有股票接口隔离，样例验证交易日和单位。"""
from datetime import date
from unittest.mock import Mock

import httpx
import pytest

from app.plugins.fuyao import client as fc
from app.plugins.fuyao import provider as fp


def _provider():
    provider = fp.FuyaoProvider()
    provider._client = Mock()
    provider._client.index_snapshot.return_value = ([{
        "thscode": "883421.TI", "last_price": 1281.983, "prev_price": 1253.339,
        "price_change": 28.644, "price_change_ratio_pct": 2.285415,
        "volume": 124322444000, "turnover": 2117720500000,
    }], 1793894426000)
    return provider


def test_ths_quote_units_actual_timestamp_and_whitelist():
    provider = _provider()
    row = provider.get_ths_index_quotes()[0]
    assert row["symbol"] == "883421.TI"
    assert row["change_pct"] == pytest.approx(0.02285415)
    assert row["volume"] == 1243224440
    assert row["amount"] == 2117720500000
    assert row["snapshot_time"] == 1793894426000
    assert "timestamp" not in row
    provider._client.index_snapshot.assert_called_once_with(["883421.TI"])
    assert "ths_index" in provider.config.datasets


@pytest.mark.parametrize("change", [
    {"price_change_ratio_pct": 99}, {"last_price": float("nan")}, {"prev_price": 0},
])
def test_invalid_quote_financial_values_fail_closed(change):
    provider = _provider()
    rows, _ = provider._client.index_snapshot.return_value
    rows[0].update(change)
    with pytest.raises(fc.FuyaoError):
        provider.get_ths_index_quotes()


def test_missing_source_timestamp_never_becomes_current_time():
    provider = _provider()
    rows, _ = provider._client.index_snapshot.return_value
    provider._client.index_snapshot.return_value = (rows, 0)
    with pytest.raises(fc.FuyaoError, match="时间"):
        provider.get_ths_index_quotes()


def test_foreign_or_empty_snapshot_never_fills_full_a_quote():
    provider = _provider()
    provider._client.index_snapshot.return_value = ([{"thscode": "000985.SH"}], 1793894426000)
    assert provider.get_ths_index_quotes() == []
    provider._client.index_snapshot.return_value = ([], 1793894426000)
    assert provider.get_ths_index_quotes() == []


def test_daily_uses_index_endpoint_beijing_dates_and_units():
    provider = _provider()
    day = date(2026, 11, 5)
    provider._client.index_historical_kline.return_value = [{
        "date_ms": fp._ms_of_date(day), "open_price": 1260, "high_price": 1290,
        "low_price": 1250, "close_price": 1281.983, "volume": 124322444000,
        "turnover": 2117720500000,
    }]
    result = provider.get_ths_index_daily("883421.TI", day, day)
    assert result["date"].to_list() == [day]
    assert result["close"].to_list() == [1281.983]
    assert result["volume"].to_list() == [1243224440]
    assert result["amount"].to_list() == [2117720500000]
    provider._client.index_historical_kline.assert_called_once_with("883421.TI", fp._ms_of_date(day), fp._ms_of_date(day))
    provider._client.historical_kline.assert_not_called()
    with pytest.raises(fc.FuyaoError):
        provider.get_ths_index_daily("000985.SH", day, day)


def test_index_historical_client_route_and_parameters():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"item": [{"date_ms": 1793808000000}]}})

    client = fc.FuyaoClient(api_key="test-key")
    client._http.close()
    client._http = httpx.Client(transport=httpx.MockTransport(handler), base_url=fc.BASE_URL)
    try:
        assert client.index_historical_kline("883421.TI", 1000, 2000) == [{"date_ms": 1793808000000}]
    finally:
        client.close()
    assert calls[0].url.path == "/api/a-share-index/prices/historical"
    assert dict(calls[0].url.params) == {"thscode": "883421.TI", "interval": "1d", "start": "1000", "end": "2000"}


def test_daily_malformed_prices_and_duplicates_are_not_silently_plotted():
    provider = _provider()
    day = date(2026, 11, 5)
    bar = {"date_ms": fp._ms_of_date(day), "open_price": 1260, "high_price": 1290,
           "low_price": 1250, "close_price": 1281.983, "volume": 100, "turnover": 2000}
    provider._client.index_historical_kline.return_value = [{**bar, "close_price": 9000}]
    assert provider.get_ths_index_daily("883421.TI", day, day).is_empty()
    provider._client.index_historical_kline.return_value = [bar, bar]
    with pytest.raises(fc.FuyaoError, match="重复"):
        provider.get_ths_index_daily("883421.TI", day, day)
