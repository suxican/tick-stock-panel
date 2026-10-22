"""Exercise the migrated extension against the current repository and lifespan."""
from contextlib import asynccontextmanager
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.extensions.loader import configure_backend_extensions
from app.market_time import CN_TZ
from app.services.index_const import CORE_INDEX_SYMBOLS
from app.tickflow.repository import DataStore, KlineRepository


def _register(app, monkeypatch):
    monkeypatch.setattr(
        "app.extensions.loader._custom_module_names", lambda: ["app.custom.market_game"],
    )
    registry, errors = configure_backend_extensions(app)
    assert not errors
    assert "market-game.workbench" in registry.extension_ids()


def _seed_prices(repo):
    days = [date(2026, 9, day) for day in (28, 29, 30)]
    records = []
    for index, day in enumerate(days):
        close = 10 + index * 0.1
        records.append({
            "symbol": "000001.SZ", "date": day, "open": close - 0.02,
            "close": close, "high": close + 0.1, "low": close - 0.1,
            "volume": 200_000.0, "amount": close * 20_000_000,
            "quote_ts": datetime.combine(day, datetime.min.time(), CN_TZ).replace(hour=15).timestamp() * 1000,
        })
    raw = pl.DataFrame(records)
    raw.write_parquet(repo.store.data_dir / "kline_daily" / "sample.parquet")
    enriched = raw.with_columns(*[
        pl.col(field).alias(f"raw_{field}") for field in ("close", "high", "low")
    ])
    repo.append_enriched(enriched)
    indices = pl.concat([
        raw.with_columns(pl.lit(symbol).alias("symbol")) for symbol in CORE_INDEX_SYMBOLS
    ])
    indices.write_parquet(repo.store.data_dir / "kline_index_daily" / "sample.parquet")
    pl.DataFrame({
        "symbol": ["000001.SZ"], "name": ["样本股票"], "asset_type": ["stock"],
        "listing_date": [date(2000, 1, 1)], "as_of": [days[-1]], "status": ["active"],
    }).write_parquet(repo.store.data_dir / "instruments" / "sample.parquet")
    repo.store._register_views()


def test_extension_generates_archives_and_reopens_using_current_repository(tmp_path, monkeypatch):
    fixed_now = datetime(2026, 10, 9, 16, tzinfo=CN_TZ)
    monkeypatch.setattr("app.services.market_game_snapshot.cn_now", lambda: fixed_now)
    monkeypatch.setattr("app.services.market_game_service.cn_today", lambda: fixed_now.date())

    def no_live_quotes():
        pytest.fail("historical analysis must never fetch current overseas quotes")

    monkeypatch.setattr("app.services.market_game_service.collect_overseas_quotes", no_live_quotes)
    store = DataStore(tmp_path)
    repo = KlineRepository(store)
    _seed_prices(repo)
    original = {path: path.read_bytes() for path in tmp_path.rglob("*.parquet")}
    generation_path = tmp_path / ".matrix_generation_stock.json"
    generation = generation_path.read_bytes()

    @asynccontextmanager
    async def core_lifespan(app):
        app.state.repo = repo
        yield

    app = FastAPI(lifespan=core_lifespan)
    _register(app, monkeypatch)
    try:
        with TestClient(app) as client:
            assert app.state.market_game_service.repo is repo
            assert client.get("/api/market-game/reports").json() == {"reports": []}
            response = client.post("/api/market-game/analyze", json={"as_of": "2026-09-30"})
            assert response.status_code == 200, response.text
            report = response.json()
            assert report["as_of"] == "2026-09-30"
            assert report["quality"] == "limited"  # only three historical sessions
            assert report["allocation"]["max"] == 0
            assert report["candidates"] == []  # historical metadata is not PIT-certified
            evidence = {row["id"]: row["value"] for row in report["evidence"]}
            assert evidence["stock_count"] == 1
            assert evidence["median_return"] == pytest.approx(10.2 / 10.1 - 1)
            report_url = f"/api/market-game/reports/{report['id']}"
            frozen_path = tmp_path / "user_data" / "market_game" / f"{report['id']}.json"
            frozen = frozen_path.read_bytes()
            assert client.get(report_url).json() == report
            assert client.get(report_url + "/capital").status_code == 200
            assert client.get(report_url + "/model").json()["execution"] is None
            assert frozen_path.read_bytes() == frozen
        assert app.state.market_game_service is None
        with TestClient(app) as client:
            assert client.get(report_url).json() == report
            assert client.get("/api/market-game/reports").json()["reports"][0]["id"] == report["id"]
        assert generation_path.read_bytes() == generation
        assert all(path.read_bytes() == content for path, content in original.items())
    finally:
        store.db.close()


def test_empty_repository_returns_actionable_error_without_archiving(tmp_path, monkeypatch):
    store = DataStore(tmp_path)
    app = FastAPI()
    app.state.repo = KlineRepository(store)
    _register(app, monkeypatch)
    try:
        with TestClient(app) as client:
            response = client.post("/api/market-game/analyze", json={"as_of": "2026-09-30"})
            assert response.status_code == 400
            assert client.get("/api/market-game/reports").json() == {"reports": []}
            assert not (tmp_path / "user_data" / "market_game").exists()
    finally:
        store.db.close()


def test_optional_service_initialization_failure_keeps_core_alive(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("private directory")

    monkeypatch.setattr("app.api.market_game.MarketGameService", broken)
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    @app.get("/health")
    def health():
        return {"ok": True}

    _register(app, monkeypatch)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"ok": True}
        response = client.get("/api/market-game/reports")
        assert response.status_code == 503
        assert "private" not in response.text
