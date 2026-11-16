"""盘后定版: 生成当日博弈计划(缺失时) → 存档环境结论; 全程软失败。"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl

from app.services import environment_conclusion_archive as archive
from app.services import post_close_freeze as pcf
from app.services import preferences, regime_builder
from app.services.market_game_service import AnalysisBusyError

DAY = date(2026, 11, 13)


class FakeStore:
    def __init__(self, reports):
        self.reports = reports

    def list_reports(self):
        return [{"id": r["id"], "as_of": r["as_of"]} for r in self.reports]

    def get(self, report_id):
        return next(r for r in self.reports if r["id"] == report_id)


class FakeService:
    def __init__(self, reports=(), error=None):
        self.store = FakeStore(list(reports))
        self.error = error
        self.calls = []

    def generate(self, as_of, risk=None):
        self.calls.append((as_of, risk))
        if self.error:
            raise self.error
        return {"id": "new", "as_of": DAY.isoformat()}


def _setup(tmp_path, monkeypatch, service):
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [DAY], "state": ["weak"], "score": [30], "capital_volume": ["flat"]}))
    monkeypatch.setattr(preferences, "get_env_matrix_plan",
                        lambda: {"weak_flat": {"cap_pct": 10, "styles": ["空仓观望"], "note": ""}})
    return SimpleNamespace(market_game_service=service, repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))


def _versions(tmp_path):
    return (archive.load_day(tmp_path, DAY.isoformat()) or {}).get("versions", [])


def test_freeze_skips_generation_when_report_exists(tmp_path, monkeypatch):
    service = FakeService([{"id": "a", "as_of": "2026-11-13", "requested_risk": {"total_cap": 0.3},
                            "allocation": {"total_cap": 0.0, "market_gate": 1.0}}])
    out = pcf.post_close_freeze(_setup(tmp_path, monkeypatch, service))
    assert service.calls == []
    assert out == {"date": "2026-11-13", "generated": False, "skipped_reason": "当日计划已存在", "archived": True}
    assert [v["trigger"] for v in _versions(tmp_path)] == ["pipeline"]


def test_freeze_generates_with_latest_requested_risk(tmp_path, monkeypatch):
    risk = {"total_cap": 0.3, "single_cap": 0.1}
    service = FakeService([{"id": "b", "as_of": "2026-11-12", "requested_risk": risk},
                           {"id": "c", "as_of": "2026-11-11", "requested_risk": {"total_cap": 0.9}}])
    out = pcf.post_close_freeze(_setup(tmp_path, monkeypatch, service))
    assert service.calls == [(None, risk)]
    assert out["generated"] and out["archived"]


def test_freeze_uses_default_risk_without_reports(tmp_path, monkeypatch):
    service = FakeService([])
    pcf.post_close_freeze(_setup(tmp_path, monkeypatch, service))
    assert service.calls == [(None, None)]


def test_freeze_skips_when_generation_busy(tmp_path, monkeypatch):
    service = FakeService([], error=AnalysisBusyError("busy"))
    out = pcf.post_close_freeze(_setup(tmp_path, monkeypatch, service))
    assert not out["generated"] and out["skipped_reason"] == "已有分析正在生成"
    assert out["archived"]


def test_freeze_survives_generation_failure(tmp_path, monkeypatch):
    service = FakeService([], error=RuntimeError("snapshot failed"))
    out = pcf.post_close_freeze(_setup(tmp_path, monkeypatch, service))
    assert not out["generated"] and "snapshot failed" in out["skipped_reason"]
    assert out["archived"]


def test_freeze_without_market_game_service_still_archives(tmp_path, monkeypatch):
    out = pcf.post_close_freeze(_setup(tmp_path, monkeypatch, None))
    assert out["skipped_reason"] == "博弈服务未加载" and out["archived"]


def test_freeze_without_regime_history_does_nothing(tmp_path, monkeypatch):
    service = FakeService([])
    state = SimpleNamespace(market_game_service=service, repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    out = pcf.post_close_freeze(state)
    assert out == {"date": None, "generated": False, "skipped_reason": "暂无环境历史", "archived": False}
    assert service.calls == []


def test_scheduled_pipeline_runs_freeze_after_lab(monkeypatch):
    from app.jobs import daily_pipeline

    order = []
    monkeypatch.setattr(daily_pipeline, "_run_tracked", lambda fn, name: True)
    monkeypatch.setattr("app.services.lab_schedule.run_scheduled_lab", lambda state: order.append("lab"))
    monkeypatch.setattr(pcf, "post_close_freeze", lambda state: order.append("freeze") or {})
    daily_pipeline._scheduled_pipeline_task(lambda: None)
    assert order == ["lab", "freeze"]


def test_scheduled_pipeline_freeze_failure_is_soft(monkeypatch):
    from app.jobs import daily_pipeline

    monkeypatch.setattr(daily_pipeline, "_run_tracked", lambda fn, name: True)
    monkeypatch.setattr("app.services.lab_schedule.run_scheduled_lab", lambda state: None)

    def boom(state):
        raise RuntimeError("freeze failed")

    monkeypatch.setattr(pcf, "post_close_freeze", boom)
    daily_pipeline._scheduled_pipeline_task(lambda: None)  # must not raise



def test_freeze_regenerates_when_same_day_report_lacks_market_gate(tmp_path, monkeypatch):
    risk = {"total_cap": 0.3}
    service = FakeService([{"id": "a", "as_of": "2026-11-13", "requested_risk": risk,
                            "allocation": {"total_cap": 0.0}}])
    out = pcf.post_close_freeze(_setup(tmp_path, monkeypatch, service))
    assert service.calls == [(None, risk)] and out["generated"]
