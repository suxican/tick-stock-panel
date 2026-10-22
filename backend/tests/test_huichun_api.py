"""Exercise Huichun HTTP contracts against its real persisted service."""

import json
from datetime import date

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.huichun import router
from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture
def huichun_client(tmp_path):
    store = DataStore(tmp_path / "data")
    app = FastAPI()
    app.state.datastore = store
    app.state.repo = KlineRepository(store)
    app.include_router(router)
    with TestClient(app) as client:
        yield client, app.state.huichun_mode_service
    store.db.close()


def test_config_success_conflict_and_empty_reads(huichun_client):
    client, _ = huichun_client
    config = client.get("/api/huichun/config")
    assert config.status_code == 200
    assert config.json()["revision"] == 0
    assert client.get("/api/huichun/snapshot").json()["scan"] is None
    tracking = client.get("/api/huichun/tracking")
    assert tracking.status_code == 200
    assert tracking.json()["records"] == []
    body = {"rules": {"rally_threshold": 0.55}, "expected_revision": 0}
    saved = client.put("/api/huichun/config", json=body)
    assert saved.status_code == 200
    assert saved.json()["revision"] == 1
    assert saved.json()["rules"]["rally_threshold"] == 0.55
    conflict = client.put("/api/huichun/config", json=body)
    assert conflict.status_code == 409
    assert "版本已变化" in conflict.json()["detail"]
    assert client.get("/api/huichun/config").json() == saved.json()


@pytest.mark.parametrize(("method", "path", "body"), [
    ("put", "/config", {"rules": {"ma_window": 1}, "expected_revision": 0}),
    ("put", "/config", {"rules": {}, "expected_revision": True}),
    ("put", "/config", {"rules": {"unknown_rule": 1}, "expected_revision": 0}),
    ("post", "/scan", {"start_date": "2026-10-20", "end_date": "2026-10-12"}),
    ("post", "/scan", {"end_date": "not-a-date"}),
    ("post", "/chart", {"symbol": "600000.SH", "as_of": "2026-10-12", "rules": {}, "rule_revision": -1}),
    ("post", "/chart", {"symbol": "../outside", "as_of": "2026-10-12", "rules": {}, "rule_revision": 0}),
    ("post", "/tracking", {"scan_id": "../outside", "candidate_ids": ["x"]}),
    ("post", "/tracking", {"scan_id": "a" * 32, "candidate_ids": []}),
])
def test_invalid_http_inputs_are_rejected_without_persistence(huichun_client, method, path, body):
    client, service = huichun_client
    response = client.request(method, f"/api/huichun{path}", json=body)
    assert response.status_code == 422
    assert not service.root.exists()


def test_unknown_scan_and_tracking_return_client_errors(huichun_client):
    client, _ = huichun_client
    response = client.post("/api/huichun/tracking", json={
        "scan_id": "a" * 32, "candidate_ids": ["missing"],
    })
    assert response.status_code == 400
    assert "筛选结果不存在" in response.json()["detail"]
    response = client.delete("/api/huichun/tracking/missing")
    assert response.status_code == 400
    assert "跟踪记录不存在" in response.json()["detail"]


def test_scan_acceptance_reports_missing_data_through_snapshot(huichun_client):
    client, service = huichun_client
    response = client.post("/api/huichun/scan", json={})
    assert response.status_code == 202
    assert response.json()["job"]["kind"] == "scan"
    service.wait(timeout=10)
    snapshot = client.get("/api/huichun/snapshot")
    assert snapshot.status_code == 200
    assert snapshot.json()["job"]["status"] == "failed"
    assert "日线" in snapshot.json()["job"]["error"]
    assert snapshot.json()["scan"] is None


def test_tracking_refresh_without_records_is_an_accepted_completed_job(huichun_client):
    client, service = huichun_client
    response = client.post("/api/huichun/tracking/refresh")
    assert response.status_code == 202
    assert service.wait(timeout=10)["job"]["status"] == "completed"
    assert client.get("/api/huichun/tracking").json()["records"] == []


def test_tracking_membership_add_and_remove_use_the_persisted_scan(huichun_client):
    client, service = huichun_client
    scan_id = "a" * 32
    service.root.mkdir(parents=True)
    (service.root / f"scan-{scan_id}.json").write_text(json.dumps({
        "rule_revision": 0, "rules": service.get_config()["rules"], "candidates": [{
            "id": "candidate", "symbol": "600001.SH", "name": "测试股票",
            "signal_date": "2026-10-12", "raw_close": 10.0, "adjusted_close": 5.0,
            "baseline_fingerprint": "persisted-baseline",
        }],
    }), encoding="utf-8")
    invalid = client.post("/api/huichun/tracking", json={
        "scan_id": scan_id, "candidate_ids": ["not-in-scan"],
    })
    assert invalid.status_code == 400
    assert client.get("/api/huichun/tracking").json()["records"] == []
    added = client.post("/api/huichun/tracking", json={
        "scan_id": scan_id, "candidate_ids": ["candidate", "candidate"],
    })
    assert added.status_code == 200
    service.wait(timeout=10)
    records = client.get("/api/huichun/tracking").json()["records"]
    assert len(records) == 1
    assert records[0]["base_close"] == 10.0
    assert records[0]["base_adjusted_close"] == 5.0
    removed = client.delete("/api/huichun/tracking/candidate")
    assert removed.status_code == 200
    assert removed.json()["records"] == []


def test_unready_service_returns_503_without_breaking_other_routes():
    app = FastAPI()
    app.include_router(router)

    @app.get("/health")
    def health():
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/api/huichun/config")
        assert response.status_code == 503
        assert "服务未就绪" in response.json()["detail"]
        assert client.get("/health").json() == {"ok": True}


def test_storage_failure_returns_sanitized_503(huichun_client, monkeypatch):
    client, service = huichun_client

    def unavailable():
        raise OSError("private storage path / secret-token")

    monkeypatch.setattr(service, "get_tracking", unavailable)
    response = client.get("/api/huichun/tracking")
    assert response.status_code == 503
    assert "暂时不可用" in response.json()["detail"]
    assert "secret-token" not in response.text
    assert "private storage" not in response.text


def test_chart_is_read_only_and_does_not_fall_back_without_data(huichun_client):
    client, service = huichun_client
    response = client.post("/api/huichun/chart", json={
        "symbol": "600000.SH", "as_of": "2026-10-12", "rules": {}, "rule_revision": 0,
    })
    assert response.status_code == 200
    chart = response.json()
    assert chart["source"] == "huichun_a0"
    assert chart["rows"] == []
    assert chart["huichun"]["macd"] == [10, 20, 9]
    assert chart["huichun"]["as_of"] == "2026-10-12"
    assert chart["huichun"]["data_status"] == "no_valid_data"
    assert client.get("/api/huichun/snapshot").json()["scan"] is None
    assert not service.root.exists()


def test_chart_maps_future_cutoff_and_storage_errors(huichun_client, monkeypatch):
    client, service = huichun_client
    body = {"symbol": "600000.SH", "as_of": "2999-01-01", "rules": {}, "rule_revision": 0}
    response = client.post("/api/huichun/chart", json=body)
    assert response.status_code == 400
    assert "已收盘" in response.json()["detail"]

    def unavailable():
        raise OSError("private path / secret-token")

    monkeypatch.setattr(service, "_input_signature", unavailable)
    body["as_of"] = "2026-10-12"
    response = client.post("/api/huichun/chart", json=body)
    assert response.status_code == 503
    assert "secret-token" not in response.text


def test_chart_serializes_real_a0_indicators_and_frozen_custom_rules(huichun_client):
    client, service = huichun_client
    days = [date(2026, 10, day) for day in (12, 13, 14)]
    raw = pl.DataFrame({
        "symbol": ["600000.SH"] * 3, "date": days,
        "open": [10.0, 11.0, 12.0], "close": [10.0, 11.0, 12.0],
        "high": [10.1, 11.1, 12.1], "low": [9.9, 10.9, 11.9],
        "volume": [1000.0] * 3, "amount": [10000.0] * 3,
    })
    store = client.app.state.datastore
    raw.write_parquet(store.data_dir / "kline_daily/chart.parquet")
    store._register_views()
    response = client.post("/api/huichun/chart", json={
        "symbol": "600000.SH", "as_of": "2026-10-14",
        "rules": {"ma_window": 90}, "rule_revision": 7,
    })
    assert response.status_code == 200
    result = response.json()
    assert [row["date"] for row in result["rows"]] == [str(day) for day in days]
    assert result["rows"][1]["macd_dif"] == pytest.approx(2 / 11 - 2 / 21)
    assert result["rows"][1]["macd_hist"] == pytest.approx(2 * (2 / 11 - 2 / 21) * .8)
    assert result["huichun"]["rules"]["ma_window"] == 90
    assert result["huichun"]["rule_revision"] == 7
    assert service.get_config()["rules"]["ma_window"] == 60
    assert not service.root.exists()


def test_chart_reports_unready_service():
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post("/api/huichun/chart", json={
            "symbol": "600000.SH", "as_of": "2026-10-12", "rules": {}, "rule_revision": 0,
        })
        assert response.status_code == 503
        assert "服务未就绪" in response.json()["detail"]
