from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.first_board import router
from app.services.first_board import FirstBoardService
from app.services.first_board_context import evaluate_sectors


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


def test_snapshot_exposes_industry_coverage_and_block_reason(client):
    members = {f"{600000 + i}.SH": "金融-银行" for i in range(100)}
    quotes = pl.DataFrame({"symbol": list(members)[:3], "change_pct": [.08, .06, .01]})
    sector = evaluate_sectors(quotes, members)["金融-银行"]
    service = client.app.state.first_board_service
    service._snapshot["rows"] = [{
        "symbol": "600000.SH", "sector_confirmation": sector,
        "can_buy": False, "event_id": None, "suggested_amount": None,
        "blocked_reasons": [sector["reason"]],
    }]
    response = client.get("/api/first-board/snapshot")
    assert response.status_code == 200
    row = response.json()["rows"][0]
    assert row["sector_confirmation"]["member_count"] == 100
    assert row["sector_confirmation"]["count"] == 3
    assert row["sector_confirmation"]["coverage"] == .03
    assert row["sector_confirmation"]["confirmed"] is False
    assert "覆盖率" in row["blocked_reasons"][0]
