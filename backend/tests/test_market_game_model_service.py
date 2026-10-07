import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.market_game import router
from app.services.market_game_model_service import MarketGameModelService, ModelBusyError
from app.services.market_game_service import MarketGameService


def service(tmp_path, monkeypatch):
    snapshot = {
        "as_of": "2026-09-30", "cutoff": "2026-09-30T16:00:00+08:00",
        "input_version": "fixed", "quality": "unavailable", "research_only": True,
        "metadata_scope": "current_observation", "metrics": {}, "previous": {},
        "stocks": [], "history": [], "evidence": [], "limitations": [],
    }
    monkeypatch.setattr("app.services.market_game_snapshot.build_snapshot", lambda *args: snapshot.copy())
    monkeypatch.setattr("app.services.market_game_service.collect_overseas_quotes", lambda: None)
    value = MarketGameService(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), data_dir=tmp_path)
    return value, value.generate(None)


def test_new_report_freezes_model_policy_but_does_not_change_allocation(tmp_path, monkeypatch):
    value, report = service(tmp_path, monkeypatch)
    assert report["ecology"]["shadow_only"] is True
    assert report["feedback_hypothesis"]["entry_authorized"] is False
    assert report["execution_policy"]["costs"]["account_equity"] == 1_000_000
    assert report["allocation"]["max"] == 0 and report["candidates"] == []
    assert value.get_report(report["id"]) == report
    assert value.store.snapshot(report["id"])["input_version"] == report["input_version"]


def test_read_only_empty_model_and_refresh_survive_restart(tmp_path, monkeypatch):
    value, report = service(tmp_path, monkeypatch)
    path = value.store.root / f"{report['id']}.json"
    frozen = path.read_bytes()
    before_files = set(value.store.root.iterdir())
    empty = value.model(report["id"])
    assert empty["execution"] is None and empty["evaluated_at"] is None
    assert set(value.store.root.iterdir()) == before_files
    now = datetime.fromisoformat("2026-10-08T16:00:00+08:00")
    result = value.model_evaluations.evaluate(report["id"], now=now)
    assert result["execution"]["entry_authorized"] is False
    assert result["execution"]["status"] == "unavailable"
    assert result["adaptation"]["automatic_adjustment"] is False
    assert result["adaptation"]["groups"] == []
    assert path.read_bytes() == frozen
    journal = next(value.store.root.glob(f"{report['id']}.model.*.json"))
    archived = json.loads(journal.read_text(encoding="utf-8"))
    assert archived["execution_inputs"]["error"]
    restored = MarketGameModelService(value.repo, value.store)
    def forbidden(*args, **kwargs):
        pytest.fail("GET cannot rebuild model inputs")
    monkeypatch.setattr("app.services.market_game_model_service.build_execution_evidence", forbidden)
    assert restored.get(report["id"]) == result


def test_legacy_report_not_retrofitted_with_execution_or_ecology(tmp_path, monkeypatch):
    value, report = service(tmp_path, monkeypatch)
    old = {key: item for key, item in report.items() if key not in {"ecology", "feedback_hypothesis", "execution_policy"}}
    legacy = value.store.save(old, value.store.snapshot(report["id"]))
    loaded = value.get_report(legacy["id"])
    assert loaded["ecology"] is None and loaded["execution_policy"] is None
    result = value.model_evaluations.evaluate(legacy["id"], now=datetime.fromisoformat("2026-10-08T16:00:00+08:00"))
    assert result["execution"]["status"] == "unavailable"
    assert result["feedback"] == []
    assert value.store.get(legacy["id"]) == legacy


def test_model_failure_preserves_last_result_and_releases_busy_flag(tmp_path, monkeypatch):
    value, report = service(tmp_path, monkeypatch)
    now = datetime.fromisoformat("2026-10-08T16:00:00+08:00")
    first = value.model_evaluations.evaluate(report["id"], now=now)
    def fail(*args, **kwargs):
        raise ValueError("test data unavailable")
    monkeypatch.setattr("app.services.market_game_model_service.build_execution_evidence", fail)
    for _ in range(2):
        with pytest.raises(ValueError):
            value.model_evaluations.evaluate(report["id"], now=now)
    assert value.model(report["id"]) == first
    assert len(value.store.model_evaluations(report["id"])) == 1


def test_parallel_model_refresh_is_nonblocking(tmp_path, monkeypatch):
    value, report = service(tmp_path, monkeypatch)
    started, resume = Event(), Event()
    def wait_for_data(*args, **kwargs):
        assert value.model_evaluations._guard.acquire(blocking=False)
        value.model_evaluations._guard.release()
        started.set()
        assert resume.wait(5)
        return {"sessions": {}, "error": "no data"}
    monkeypatch.setattr("app.services.market_game_model_service.build_execution_evidence", wait_for_data)
    now = datetime.fromisoformat("2026-10-08T16:00:00+08:00")
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(value.model_evaluations.evaluate, report["id"], now=now)
        try:
            assert started.wait(3)
            with pytest.raises(ModelBusyError):
                value.model_evaluations.evaluate(report["id"], now=now)
        finally:
            resume.set()
        assert future.result(timeout=5)["report_id"] == report["id"]


def test_future_cutoff_rejected_without_writing(tmp_path, monkeypatch):
    value, report = service(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        value.model_evaluations.evaluate(report["id"], now=datetime.fromisoformat("2026-09-29T16:00:00+08:00"))
    assert value.store.model_evaluations(report["id"]) == []


def test_http_model_refresh_runs_real_service_and_persists_contract(tmp_path, monkeypatch):
    value, report = service(tmp_path, monkeypatch)
    app = FastAPI()
    app.include_router(router)
    app.state.market_game_service = value
    client = TestClient(app)
    base = f"/api/market-game/reports/{report['id']}"
    assert client.get(f"{base}/model").json()["execution"] is None
    response = client.post(f"{base}/evaluate-model")
    assert response.status_code == 200
    result = response.json()
    assert result["report_id"] == report["id"]
    assert result["execution"]["kind"] == "simulated_execution"
    assert result["execution"]["entry_authorized"] is False
    assert result["adaptation"]["validation_status"] == "not_validated"
    assert client.get(f"{base}/model").json() == result
    assert client.get(base).json() == report
