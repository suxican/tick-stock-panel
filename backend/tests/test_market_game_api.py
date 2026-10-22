from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.market_game import router
from app.extensions.loader import configure_backend_extensions


def client_for(service=None):
    app = FastAPI()
    app.include_router(router)
    if service is not None:
        app.state.market_game_service = service
    return TestClient(app)


@pytest.mark.parametrize("body", [
    {"risk": {"total_cap": 2}}, {"risk": {"single_cap": -1}},
    {"risk": {"max_candidates": 6}}, {"risk": {"max_candidates": True}},
    {"risk": {"risk_per_trade": 0.1}}, {"risk": {"min_amount": 0}},
    {"risk": {"portfolio_risk_budget": 0.11}}, {"risk": {"portfolio_risk_budget": True}},
    {"risk": {"total_cap": True}}, {"as_of": "bad"}, {"cutoff": "2026-09-30"},
    {"risk": {"unknown": 1}},
])
def test_invalid_parameters_rejected_before_analysis(body):
    assert client_for(SimpleNamespace()).post("/api/market-game/analyze", json=body).status_code == 422


def test_analyze_uses_parsed_date_and_decimal_risk():
    def generate(as_of, risk):
        assert as_of == date(2026, 9, 30)
        assert risk["total_cap"] == 0.4
        assert risk["max_candidates"] == 5
        return {"id": "saved"}
    response = client_for(SimpleNamespace(generate=generate)).post(
        "/api/market-game/analyze", json={"as_of": "2026-09-30", "risk": {"total_cap": 0.4}},
    )
    assert response.status_code == 200 and response.json()["id"] == "saved"


def test_storage_errors_and_missing_service_are_isolated():
    assert client_for().get("/api/market-game/reports").status_code == 503
    def fail():
        raise OSError("private/path/secret")
    response = client_for(SimpleNamespace(list_reports=fail)).get("/api/market-game/reports")
    assert response.status_code == 503 and "private/path" not in response.text


def test_missing_report_and_bad_ids_have_explicit_responses():
    def get(report_id):
        raise FileNotFoundError("private/path")
    response = client_for(SimpleNamespace(get_report=get)).get("/api/market-game/reports/missing")
    assert response.status_code == 404 and "private/path" not in response.text


def test_extension_registers_without_core_changes(monkeypatch):
    monkeypatch.setattr("app.extensions.loader._custom_module_names", lambda: ["app.custom.market_game"])
    app = FastAPI()
    registry, errors = configure_backend_extensions(app)
    assert not errors and "market-game.workbench" in registry.extension_ids()
    assert TestClient(app).get("/api/market-game/reports").status_code == 503


def test_ai_failure_is_separate_from_saved_report():
    async def explain(_):
        raise RuntimeError("AI token private")
    response = client_for(SimpleNamespace(explain=explain)).post("/api/market-game/reports/abc/explain")
    assert response.status_code == 503 and "private" not in response.text


def test_model_read_and_explicit_refresh_are_separate():
    calls = []
    def read(report_id):
        calls.append(("read", report_id))
        return {"report_id": report_id, "execution": None}
    def refresh(report_id):
        calls.append(("refresh", report_id))
        return {"report_id": report_id, "execution": {"kind": "simulated_execution"}}
    client = client_for(SimpleNamespace(model=read, evaluate_model=refresh))
    assert client.get("/api/market-game/reports/example/model").json()["execution"] is None
    assert client.post("/api/market-game/reports/example/evaluate-model").json()["execution"]["kind"] == "simulated_execution"
    assert calls == [("read", "example"), ("refresh", "example")]


@pytest.mark.parametrize("exception,status", [(FileNotFoundError, 404), (ValueError, 400), (OSError, 503)])
def test_model_refresh_errors_are_sanitized(exception, status):
    def refresh(_):
        raise exception("private/path/token")
    response = client_for(SimpleNamespace(evaluate_model=refresh)).post("/api/market-game/reports/example/evaluate-model")
    assert response.status_code == status and "private" not in response.text
