"""市场补充表不发布个股可选列, 单表 schema 和明细仍可查询。"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import ext_data
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, rows_to_parquet
from app.services.kaipanla_catalog import presets


@pytest.mark.parametrize("persisted", [False, True])
def test_schema_all_excludes_market_tables_but_keeps_direct_queries(tmp_path, monkeypatch, persisted):
    market = next(config for config in presets() if config.id == "ext_kpl_emotion")
    stock = ExtConfig(
        "stock_heat", "个股热度", "timeseries",
        [ExtField("symbol"), ExtField("heat", "float", "热度")],
    )
    store = ExtConfigStore(tmp_path)
    store.upsert(market)
    store.upsert(stock)
    if persisted:
        rows_to_parquet(
            [{"record_id": market.id, "strong": "42", "date": "2026-09-30"}],
            market, tmp_path, date(2026, 9, 30), replace=True,
        )
        rows_to_parquet(
            [{"symbol": "600000.SH", "heat": 0.8}],
            stock, tmp_path, date(2026, 9, 30),
        )
    scanned = []
    original = ext_data._parquet_glob

    def record_glob(config, data_dir):
        scanned.append(config.id)
        return original(config, data_dir)

    monkeypatch.setattr(ext_data, "_parquet_glob", record_glob)
    app = FastAPI()
    app.include_router(ext_data.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    with TestClient(app) as client:
        all_schemas = client.get("/api/ext-data/schema-all")
        assert all_schemas.status_code == 200
        items = all_schemas.json()["items"]
        assert [item["id"] for item in items] == [stock.id]
        assert "heat" in {column["name"] for column in items[0]["columns"]}
        assert scanned == [stock.id]

        scanned.clear()
        schema = client.get(f"/api/ext-data/schema/{market.id}")
        assert schema.status_code == 200
        assert "strong" in {column["name"] for column in schema.json()["columns"]}
        assert scanned == [market.id]
        rows = client.get(f"/api/ext-data/{market.id}/rows?date=2026-09-30")
        assert rows.status_code == 200
        assert rows.json()["date"] == ("2026-09-30" if persisted else None)
        assert rows.json()["total"] == int(persisted)
        if persisted:
            assert rows.json()["rows"][0]["strong"] == "42"
