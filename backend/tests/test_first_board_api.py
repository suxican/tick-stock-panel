from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.first_board import router
from app.services.first_board import FirstBoardService


@pytest.fixture
def client(tmp_path):
    app = FastAPI()
    app.include_router(router)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.first_board_service = FirstBoardService(repo, history_loader=lambda *_: pl.DataFrame())
    return TestClient(app)


def test_config_roundtrip_and_conflict(client):
    config = client.get("/api/first-board/config").json()
    assert config["enabled"] is False
    response = client.put("/api/first-board/config", json={**config, "notify": False})
    assert response.status_code == 200
    assert response.json()["revision"] == 1
    assert client.put("/api/first-board/config", json=config).status_code == 409
    assert len(client.get("/api/first-board/versions").json()["versions"]) == 2


def test_empty_data_responses(client):
    for endpoint in ("snapshot", "events?day=2026-10-16", "research-runs"):
        response = client.get(f"/api/first-board/{endpoint}")
        assert response.status_code == 200, response.text
    assert client.get("/api/first-board/events?day=2026-10-16").json()["total"] == 0
    assert client.post("/api/first-board/refresh").json()["rows"] == []


@pytest.mark.parametrize("payload", [
    {"event_id": "bad/path", "side": "buy", "qty": 100},
    {"event_id": "abc", "side": "buy", "qty": 101},
    {"event_id": "abc", "side": "buy", "qty": True},
    {"event_id": "abc", "side": "sell", "qty": -100},
])
def test_invalid_order_payload_rejected(client, payload):
    assert client.post("/api/first-board/paper-order", json=payload).status_code == 422


def test_unknown_event_and_ambiguous_order_rejected(client):
    body = {"event_id": "abc", "side": "buy", "qty": 100}
    assert client.post("/api/first-board/paper-order", json=body).status_code == 400
    assert client.post("/api/first-board/paper-order", json={**body, "amount": 1000.0}).status_code == 400


def test_invalid_dates_and_config_rejected(client):
    assert client.get("/api/first-board/events?day=bad").status_code == 422
    assert client.post("/api/first-board/research", json={"start": "2026-10-16", "end": "2026-10-15"}).status_code == 422
    config = client.get("/api/first-board/config").json()
    assert client.put("/api/first-board/config", json={**config, "exit_time": "12:00"}).status_code == 422


def test_missing_service_returns_explicit_unavailable():
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/api/first-board/config")
    assert response.status_code == 503
    assert "未就绪" in response.json()["detail"]
