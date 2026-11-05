from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import indices, intraday
from app.services import overseas_indices
from app.services.index_const import CORE_INDEX_SYMBOLS


def _app():
    app = FastAPI()
    app.include_router(indices.router)
    app.include_router(intraday.router)
    return app


def test_index_catalog_contains_curated_markets_independent_of_quote_services():
    rows = TestClient(_app()).get("/api/index/catalog").json()["rows"]
    assert tuple(CORE_INDEX_SYMBOLS) == ("000001.SH", "399001.SZ", "399006.SZ", "000680.SH")
    assert [r["symbol"] for r in rows[:4]] == list(CORE_INDEX_SYMBOLS)
    assert {r["symbol"] for r in rows if r["market"] == "CN"} == {
        *CORE_INDEX_SYMBOLS, "000688.SH", "000016.SH", "000903.SH", "000905.SH", "000852.SH", "883421.TI",
    }
    assert {r["symbol"]: r["market"] for r in rows if r["market"] != "CN"} == {
        "^DJI": "US", "^GSPC": "US", "^IXIC": "US", "^N225": "JP", "^KS11": "KR",
    }
    assert len(rows) == len({r["symbol"] for r in rows}) == 15
    assert all(r["asset_type"] == "index" and r["name"] for r in rows)


def test_partial_realtime_cache_fills_only_missing_requested_indices():
    app = _app()
    quote_cache = pl.DataFrame({"symbol": ["000001.SH"], "last_price": [200.0], "change_pct": [0.6]})
    app.state.quote_service = SimpleNamespace(get_index_quotes=Mock(return_value=quote_cache))
    app.state.repo = Mock()
    app.state.repo.execute_all.return_value = [
        ("000688.SH", date(2026, 11, 4), 98.0, 100.0),
        ("000016.SH", date(2026, 11, 4), 123.0, None),
    ]
    symbols = ["000001.SH", "000688.SH", "000016.SH", "000905.SH"]
    body = TestClient(app).get("/api/intraday/indices", params={"symbols": ",".join(symbols)}).json()
    assert body["count"] == 3
    by_symbol = {row["symbol"]: row for row in body["rows"]}
    assert by_symbol["000001.SH"]["source"] == "realtime"
    assert by_symbol["000001.SH"]["change_pct"] == 0.6
    assert by_symbol["000688.SH"]["source"] == "index_daily"
    assert by_symbol["000688.SH"]["date"] == "2026-11-04"
    assert by_symbol["000688.SH"]["change_pct"] == -2
    assert by_symbol["000016.SH"]["change_pct"] is None
    app.state.quote_service.get_index_quotes.assert_called_once_with(symbols)
    app.state.repo.execute_all.assert_called_once()
    assert app.state.repo.execute_all.call_args.args[1] == symbols[1:]


def test_foreign_index_minute_does_not_call_domestic_data_sources():
    # No repository, session calendar or provider is needed for this unsupported request.
    response = TestClient(_app()).get("/api/index/minute", params={"symbol": "^N225", "date": "2026-11-04"})
    assert response.status_code == 200
    assert response.json()["rows"] == []
    assert response.json()["source"] == "unsupported"


def test_overseas_routes_use_selected_provider_and_market_dates(monkeypatch):
    day = "2026-11-04"
    provider = Mock()
    provider.get_overseas_index_quotes.return_value = [
        {"symbol": "^N225", "state": "ready", "session_date": day, "price": 101.5,
         "previous_close": 100, "change_pct": 0.015},
        {"symbol": "^DJI", "state": "unavailable"},
    ]
    provider.get_overseas_index_daily.return_value = [
        {"symbol": "^N225", "date": day, "open": 100, "low": 99, "high": 102, "close": 101.5}
    ]
    monkeypatch.setattr(overseas_indices.preferences, "get_overseas_data_provider", lambda: "example")
    monkeypatch.setattr(overseas_indices.custom, "provider_has_dataset", lambda s, d: (s, d) == ("example", "overseas"))
    monkeypatch.setattr(overseas_indices.custom, "get_provider", lambda _: provider)
    client = TestClient(_app())
    for _ in range(2):
        result = client.get("/api/index/overseas-quotes").json()
        assert result["source"] == "example"
        assert result["rows"] == [{"symbol": "^N225", "date": day, "last_price": 101.5, "change_pct": 1.5}]
    provider.get_overseas_index_quotes.assert_called_once_with()
    query = {"symbol": "^N225", "start_date": day, "end_date": day}
    daily = client.get("/api/index/daily", params=query).json()
    assert daily["index_info"]["market"] == "JP"
    assert daily["rows"] == [{**provider.get_overseas_index_daily.return_value[0], "volume": None}]
    provider.get_overseas_index_daily.assert_called_once_with("^N225", date.fromisoformat(day), date.fromisoformat(day))


@pytest.mark.parametrize("provider", [None, object(), Mock(get_overseas_index_quotes=Mock(side_effect=TimeoutError))])
def test_overseas_missing_capability_method_or_connection_is_isolated(monkeypatch, provider):
    monkeypatch.setattr(overseas_indices.custom, "provider_has_dataset", lambda *_: provider is not None)
    monkeypatch.setattr(overseas_indices.custom, "get_provider", lambda _: provider)
    result = TestClient(_app()).get("/api/index/overseas-quotes").json()
    assert result["rows"] == []
    assert result["reason"]
