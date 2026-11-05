"""持仓研究接口只采用服务端证据，计划与成交账本隔离。"""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import holding_analysis as api
from app.market_time import cn_now


@pytest.fixture
def client(tmp_path, monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.position_t_service = None
    inputs = {"source": "lots", "account_id": "", "accounts": [], "input_version": "v1",
              "positions": [{"key": "h_test", "symbol": "600000.SH"}], "warnings": []}
    monkeypatch.setattr(api, "read_inputs", lambda *_a, **_k: deepcopy(inputs))
    row = {"key": "h_test", "symbol": "600000.SH", "name": "测试股份", "asset_type": "stock",
           "warnings": [], "price": {"fresh": True},
           "plan": {"can_adopt": True, "expires_on": cn_now().date().isoformat(),
                    "scenarios": [{"key": "bear", "title": "风险情景",
                                   "price_trigger": {"op": "<=", "value": 9.5}}]}}
    report = {**inputs, "rows": [row], "generated_at": cn_now().isoformat(),
              "as_of_date": cn_now().date().isoformat()}
    monkeypatch.setattr(api, "analyze_holdings", lambda *_a, **_k: deepcopy(report))
    return TestClient(app), inputs, report


def test_report_plan_and_profile_versions(client):
    http, _inputs, _report = client
    overview = http.get("/api/holding-analysis/overview").json()
    assert overview["report"] is None
    assert overview["plans"] == []
    generated = http.post("/api/holding-analysis/analyze", json={"source": "lots"})
    assert generated.status_code == 200
    report = generated.json()
    body = {"report_id": report["id"], "row_key": "h_test", "expected_input_version": "v1"}
    adopted = http.post("/api/holding-analysis/plans", json=body)
    assert adopted.status_code == 200
    assert adopted.json()["plan"]["row"]["symbol"] == "600000.SH"
    assert http.post("/api/holding-analysis/plans", json=body).json() == adopted.json()
    changed = http.put("/api/holding-analysis/profiles/h_test", json={
        "source": "lots", "expected_version": 0, "profile": {"invalidation": "收盘跌破支撑"}})
    assert changed.status_code == 200
    assert changed.json()["version"] == 1
    assert http.put("/api/holding-analysis/profiles/h_test", json={
        "source": "lots", "expected_version": 0, "profile": {}}).status_code == 409
    assert http.get("/api/holding-analysis/overview").json()["plans"][0]["needs_review"]
    assert http.post("/api/holding-analysis/plans", json=body).status_code == 409


def test_stale_source_and_forged_plan_are_rejected(client):
    http, inputs, _report = client
    report = http.post("/api/holding-analysis/analyze", json={"source": "lots"}).json()
    inputs["input_version"] = "v2"
    assert http.post("/api/holding-analysis/plans", json={
        "report_id": report["id"], "row_key": "h_test", "expected_input_version": "v1"}).status_code == 409
    assert http.post("/api/holding-analysis/plans", json={
        "report_id": report["id"], "row_key": "h_test", "expected_input_version": "v2",
        "row": {"plan": {"can_adopt": True}}}).status_code == 422
    assert http.put("/api/holding-analysis/profiles/unknown", json={
        "source": "lots", "expected_version": 0, "profile": {}}).status_code == 404


def test_alert_uses_saved_raw_price_and_invalidation_disables_it(client, monkeypatch):
    from app.strategy import monitor_rules

    http, _inputs, _report = client
    app = http.app
    app.state.holding_alert_guard = SimpleNamespace(refresh=lambda: None, revoke=lambda: None)
    monkeypatch.setattr(api, "_sync_engine", lambda _request: None)
    report = http.post("/api/holding-analysis/analyze", json={"source": "lots"}).json()
    plan = http.post("/api/holding-analysis/plans", json={
        "report_id": report["id"], "row_key": "h_test", "expected_input_version": "v1"}).json()["plan"]
    bound = http.post(f"/api/holding-analysis/plans/{plan['id']}/alerts",
                      json={"expected_version": plan["version"], "scenario_key": "bear"})
    assert bound.status_code == 200
    data = bound.json()
    rule = monitor_rules.load_one(app.state.repo.store.data_dir, data["rule_id"])
    assert rule["conditions"] == [{"field": "raw_close", "op": "<=", "value": 9.5}]
    assert rule["holding_plan_id"] == plan["id"]
    assert rule["webhook_channels"] == []
    result = http.patch(f"/api/holding-analysis/plans/{plan['id']}", json={
        "expected_version": data["plan"]["version"], "status": "invalidated"})
    assert result.status_code == 200
    assert not monitor_rules.load_one(app.state.repo.store.data_dir, data["rule_id"])["enabled"]
    assert http.post(f"/api/holding-analysis/plans/{plan['id']}/alerts", json={
        "expected_version": result.json()["plan"]["version"], "scenario_key": "bear"}).status_code == 409


def test_unknown_fields_dates_and_profile_numbers_fail_closed(client):
    http, _inputs, _report = client
    for profile in ({"max_loss_pct": -1}, {"max_loss_pct": 100}, {"deep_loss_pct": 0},
                    {"opened_on": "2099-01-01"}, {"max_holding_days": 0}, {"unexpected": True}):
        assert http.put("/api/holding-analysis/profiles/h_test", json={
            "source": "lots", "expected_version": 0, "profile": profile}).status_code == 422


def test_changed_source_during_analysis_is_not_saved(client, monkeypatch):
    http, inputs, report = client

    def generate(*_args, **_kwargs):
        inputs["input_version"] = "v2"
        return deepcopy(report)

    monkeypatch.setattr(api, "analyze_holdings", generate)
    assert http.post("/api/holding-analysis/analyze", json={"source": "lots"}).status_code == 409
    assert http.get("/api/holding-analysis/overview").json()["report"] is None


def test_partial_alert_commit_remains_paused_after_refresh_and_restart(client, monkeypatch):
    from app.services.holding_alert_guard import HoldingAlertGuard
    from app.services.holding_analysis_store import HoldingAnalysisStore
    from app.strategy import monitor_rules

    http, inputs, report = client
    now = cn_now()
    report["rows"][0]["price"].update(reference_date=now.date().isoformat(), reference_close=10)
    quote_service = SimpleNamespace(
        status=lambda: {"enabled": True, "realtime_allowed": True},
        get_market_records=lambda _s: {"600000.SH": {"timestamp": now.timestamp() * 1000, "last_price": 10}},
    )
    monkeypatch.setattr("app.services.holding_alert_guard.read_inputs", lambda *_a: deepcopy(inputs))
    guard = HoldingAlertGuard(http.app.state.repo, quote_service, None, clock=lambda: now)
    http.app.state.holding_alert_guard = guard
    generated = http.post("/api/holding-analysis/analyze", json={"source": "lots"}).json()
    adopted = http.post("/api/holding-analysis/plans", json={
        "report_id": generated["id"], "row_key": "h_test", "expected_input_version": "v1"}).json()["plan"]
    original_save = monitor_rules.save_one
    writes = []

    def save(data_dir, rule):
        writes.append(rule["enabled"])
        if len(writes) == 3:
            raise OSError("rollback write failed")
        original_save(data_dir, rule)

    def sync(_request):
        raise RuntimeError("engine synchronization failed")

    monkeypatch.setattr(monitor_rules, "save_one", save)
    monkeypatch.setattr(api, "_sync_engine", sync)
    assert http.post(f"/api/holding-analysis/plans/{adopted['id']}/alerts", json={
        "expected_version": adopted["version"], "scenario_key": "bear"}).status_code == 503
    store = HoldingAnalysisStore(http.app.state.repo.store.data_dir)
    pending = store.get_plan(adopted["id"])
    rule_id = pending["alert_rule_ids"][0]
    assert pending["alert_bindings"][rule_id] == "pending"
    assert monitor_rules.load_one(http.app.state.repo.store.data_dir, rule_id)["enabled"]
    for worker in (guard, HoldingAlertGuard(http.app.state.repo, quote_service, None, clock=lambda: now)):
        worker.refresh()
        assert not worker.allow({"id": rule_id, "holding_plan_id": adopted["id"]}, now.timestamp())
    monkeypatch.setattr(monitor_rules, "save_one", original_save)
    monkeypatch.setattr(api, "_sync_engine", lambda _r: None)
    retried = http.post(f"/api/holding-analysis/plans/{adopted['id']}/alerts", json={
        "expected_version": pending["version"], "scenario_key": "bear"})
    assert retried.status_code == 200
    assert guard.allow({"id": rule_id, "holding_plan_id": adopted["id"]}, now.timestamp())


def test_overview_reads_shared_source_once_for_many_saved_plans(client, monkeypatch):
    from app.services.holding_analysis_store import HoldingAnalysisStore

    http, inputs, _report = client
    generated = http.post("/api/holding-analysis/analyze", json={"source": "lots"}).json()
    plan = http.post("/api/holding-analysis/plans", json={
        "report_id": generated["id"], "row_key": "h_test", "expected_input_version": "v1"}).json()["plan"]
    calls = []

    def read(*_args, **_kwargs):
        calls.append(True)
        return deepcopy(inputs)

    monkeypatch.setattr(api, "read_inputs", read)
    monkeypatch.setattr(HoldingAnalysisStore, "list_plans", lambda *_a: [deepcopy(plan) for _ in range(50)])
    result = http.get("/api/holding-analysis/overview")
    assert result.status_code == 200
    assert len(result.json()["plans"]) == 50
    assert len(calls) == 1


def test_late_ai_response_is_rejected_after_source_changes(client, monkeypatch):
    http, inputs, _report = client
    generated = http.post("/api/holding-analysis/analyze", json={"source": "lots"}).json()

    async def interpret(_report):
        inputs["input_version"] = "v2"
        return {"summary": "依据", "observations": [], "limitations": []}

    monkeypatch.setattr(api, "interpret_report", interpret)
    assert http.post(f"/api/holding-analysis/reports/{generated['id']}/interpret").status_code == 409
