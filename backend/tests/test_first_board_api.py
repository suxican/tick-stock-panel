from datetime import datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.first_board import router
from app.market_time import CN_TZ
from app.services import first_board as fb
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


def test_snapshot_filters_minimum_change_and_applies_saved_revision(client, monkeypatch):
    now = datetime(2026, 10, 16, 10, 0, tzinfo=CN_TZ)
    monkeypatch.setattr(fb, "_now", lambda: now)
    service = client.app.state.first_board_service
    prices = {"600000.SH": 9.991, "600001.SH": 10.499, "600002.SH": 10.5, "600003.SH": 10.8}
    candidates = pl.DataFrame([{
        "symbol": symbol, "name": symbol, "pattern": "platform", "pattern_label": "平台突破",
        "reference_date": now.date() - timedelta(days=1), "reference_price": 10.0,
        "reference_adjusted_close": 10.0, "breakout_price": 10.2,
        "history_bars": 65, "first_board_count": 0,
    } for symbol in prices])

    def prepare(_day, _config):
        service._candidates = candidates
        service._environment = {"allowed": True, "date": "2026-10-15", "state": "range"}
        service._sectors = {}

    monkeypatch.setattr(service, "_prepare", prepare)
    monkeypatch.setattr(service._attention_data, "minutes", lambda *_args, **_kwargs: (
        pl.DataFrame(), False, "测试无分钟数据",
    ))
    quotes = pl.DataFrame([{
        "symbol": symbol, "name": symbol, "date": now.date(),
        "quote_ts": int(now.timestamp() * 1000), "raw_close": price, "close": price,
        "raw_high": price, "prev_close": 10.0, "change_pct": price / 10.0 - 1,
        "limit_up_price": 10.995, "turnover_rate": 5.0, "amount": 80_000_000.0,
    } for symbol, price in prices.items()])

    config = client.get("/api/first-board/config").json()
    response = client.put("/api/first-board/config", json={
        **config, "enabled": True, "notify": False, "require_sector_confirmation": False,
        "rules": {**config["rules"], "min_change_pct": .05},
    })
    assert response.status_code == 200
    saved = response.json()
    assert saved["rules"]["min_change_pct"] == .05
    service.process_snapshot(quotes, now.date(), now=now)
    response = client.get("/api/first-board/snapshot")
    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["revision"] == saved["revision"]
    assert {row["symbol"] for row in snapshot["rows"]} == {"600002.SH", "600003.SH"}
    assert snapshot["coverage"] == {
        "candidate_count": 2, "symbol_count": 2, "quote_count": 4, "fresh_count": 4,
    }

    response = client.put("/api/first-board/config", json={
        **saved, "rules": {**saved["rules"], "min_change_pct": .06},
    })
    assert response.status_code == 200
    revision = response.json()["revision"]
    assert revision == saved["revision"] + 1
    reset = client.get("/api/first-board/snapshot").json()
    assert reset["revision"] == revision
    assert reset["rows"] == []
    service.process_snapshot(quotes, now.date(), now=now)
    response = client.get("/api/first-board/snapshot")
    assert response.status_code == 200
    refreshed = response.json()
    assert refreshed["revision"] == revision
    assert [row["symbol"] for row in refreshed["rows"]] == ["600003.SH"]
    assert refreshed["coverage"] == {
        "candidate_count": 1, "symbol_count": 1, "quote_count": 4, "fresh_count": 4,
    }


def test_empty_data_responses(client):
    for endpoint in ("snapshot", "events?day=2026-10-16", "research-runs"):
        response = client.get(f"/api/first-board/{endpoint}")
        assert response.status_code == 200, response.text
    assert client.get("/api/first-board/events?day=2026-10-16").json()["total"] == 0
    assert client.post("/api/first-board/refresh").json()["rows"] == []


def test_events_filter_before_limit_and_preserve_order(client, monkeypatch):
    records = [{"id": str(i), "pattern": "platform", "event_type": "watch"} for i in range(5001)]
    records += [
        {"id": "buy", "pattern": "oversold", "state": "approaching", "event_type": "buy_candidate"},
        {"id": "near", "pattern": "oversold", "state": "approaching", "event_type": "approaching"},
        {"id": "exit", "pattern": "exit", "event_type": "exit_candidate"},
    ]
    monkeypatch.setattr(client.app.state.first_board_service, "events", lambda _day: records)
    base = "/api/first-board/events?day=2026-10-16"
    assert client.get(base).json()["total"] == 5004
    assert len(client.get(base).json()["events"]) == 5000
    result = client.get(base + "&pattern=oversold").json()
    assert [row["id"] for row in result["events"]] == ["buy", "near"]
    assert result["total"] == 2
    for query, ids in [
        ("event_type=approaching", ["near"]),
        ("pattern=oversold&event_type=buy_candidate", ["buy"]),
        ("pattern=exit&event_type=exit_candidate", ["exit"]),
        ("pattern=trend&event_type=approaching", []),
    ]:
        result = client.get(base + "&" + query).json()
        assert [row["id"] for row in result["events"]] == ids
        assert result["total"] == len(ids)
    assert records[0]["id"] == "0"


@pytest.mark.parametrize("query", ["pattern=unknown", "event_type=unknown"])
def test_invalid_event_filters_rejected(client, query):
    assert client.get("/api/first-board/events?" + query).status_code == 422


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
