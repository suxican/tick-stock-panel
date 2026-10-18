"""Targeted quotes reuse the shared stock cache without historical recomputation."""
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import screener


@pytest.fixture
def quotes(monkeypatch):
    frame = pl.DataFrame({
        "symbol": ["600001.SH", "000001.SZ", "600002.SH"],
        "name": ["样本一", "样本二", "样本三"],
        "close": [5.0, 8.0, 9.0],
        "raw_close": [10.0, float("nan"), 18.0],
        "change_pct": [0.05, float("inf"), -0.02],
        "date": [date(2026, 10, 9)] * 3,
        "private_column": [1, 2, 3],
    })
    repo = SimpleNamespace(get_enriched_latest=Mock(return_value=(frame, date(2026, 10, 9))))
    slow_path = Mock(side_effect=AssertionError("targeted quotes must not recompute history"))
    monkeypatch.setattr(screener, "ScreenerService", slow_path)
    app = FastAPI()
    app.state.repo = repo
    app.include_router(screener.router)
    with TestClient(app) as client:
        yield client, repo, slow_path


def test_selected_quotes_preserve_raw_price_date_and_decimal_units(quotes):
    client, repo, slow_path = quotes
    response = client.get("/api/screener/market-snapshot", params={
        "symbols": "600001.SH,000001.SZ,600001.SH",
    })
    assert response.status_code == 200
    assert response.json() == {"as_of": "2026-10-09", "rows": [
        {"symbol": "600001.SH", "name": "样本一", "close": 5.0, "raw_close": 10.0,
         "change_pct": 0.05, "date": "2026-10-09"},
        {"symbol": "000001.SZ", "name": "样本二", "close": 8.0, "raw_close": None,
         "change_pct": None, "date": "2026-10-09"},
    ]}
    repo.get_enriched_latest.assert_called_once()
    slow_path.assert_not_called()


@pytest.mark.parametrize("symbols", ["", " , "])
def test_explicit_empty_symbols_does_not_load_market(quotes, symbols):
    client, repo, slow_path = quotes
    response = client.get("/api/screener/market-snapshot", params={"symbols": symbols})
    assert response.status_code == 200
    assert response.json() == {"as_of": None, "rows": []}
    repo.get_enriched_latest.assert_not_called()
    slow_path.assert_not_called()


@pytest.mark.parametrize("symbols", [
    "600001", "600001.US", "600001.SH'); DROP TABLE kline_daily; --",
    ",".join(f"{number:06d}.SH" for number in range(101)),
])
def test_invalid_symbols_and_oversized_batch_fail_before_reading(quotes, symbols):
    client, repo, _ = quotes
    response = client.get("/api/screener/market-snapshot", params={"symbols": symbols})
    assert response.status_code == 422
    repo.get_enriched_latest.assert_not_called()


def test_one_hundred_symbols_and_duplicate_codes_are_supported(quotes):
    client, repo, _ = quotes
    symbols = [f"{number:06d}.SH" for number in range(100)]
    response = client.get("/api/screener/market-snapshot", params={"symbols": ",".join(symbols * 2)})
    assert response.status_code == 200
    assert response.json() == {"as_of": "2026-10-09", "rows": []}
    repo.get_enriched_latest.assert_called_once()


def test_empty_cache_returns_empty_quotes(quotes):
    client, repo, _ = quotes
    repo.get_enriched_latest.return_value = (pl.DataFrame(), None)
    response = client.get("/api/screener/market-snapshot", params={"symbols": "600001.SH"})
    assert response.json() == {"as_of": None, "rows": []}


def test_missing_raw_quote_is_not_substituted_with_adjusted_close(quotes):
    client, repo, _ = quotes
    repo.get_enriched_latest.return_value = (
        pl.DataFrame({"symbol": ["600001.SH"], "close": [5.0]}), date(2026, 10, 9),
    )
    response = client.get("/api/screener/market-snapshot", params={"symbols": "600001.SH"})
    assert response.json()["rows"] == [{"symbol": "600001.SH", "close": 5.0}]


def test_no_symbols_parameter_preserves_full_market_contract(quotes, monkeypatch):
    client, repo, _ = quotes
    service = SimpleNamespace(
        latest_date=lambda: date(2026, 10, 9),
        _load_enriched_for_date=lambda day: pl.DataFrame({
            "symbol": ["600001.SH"], "close": [5.0], "change_pct": [0.05],
            "total_shares": [100.0], "float_shares": [80.0],
        }),
    )
    monkeypatch.setattr(screener, "ScreenerService", lambda repository: service)
    response = client.get("/api/screener/market-snapshot")
    assert response.status_code == 200
    row = response.json()["rows"][0]
    assert row["market_cap"] == 500.0
    assert row["float_market_cap"] == 400.0
    repo.get_enriched_latest.assert_not_called()
