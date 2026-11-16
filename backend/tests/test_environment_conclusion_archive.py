"""环境结论存档: 每个交易日一份文件, 多版只追加, 去重, 损坏不覆盖。"""
from __future__ import annotations

import json

import pytest

from app.services import environment_conclusion as ec
from app.services import environment_conclusion_archive as archive


def _conclusion(**overrides):
    base = {
        "date": "2026-11-13", "cell": "weak_flat", "sentiment": "weak", "volume": "flat", "missing_reason": None,
        "plan": {"cap_pct": 10, "styles": ["空仓观望"], "note": ""}, "gate": None,
        "effective_cap_pct": 10, "limited_by": "plan", "evidence": None, "evidence_as_of": None,
    }
    return {**base, **overrides}


def _path(tmp_path, day="2026-11-13"):
    return tmp_path / "user_data" / archive.ARCHIVE_DIR / f"{day}.json"


def test_first_write_and_append(tmp_path):
    assert archive.append_version(tmp_path, _conclusion(), "pipeline")
    assert archive.append_version(tmp_path, _conclusion(effective_cap_pct=20, plan={"cap_pct": 20, "styles": [], "note": ""}), "preset")
    day = archive.load_day(tmp_path, "2026-11-13")
    assert day["date"] == "2026-11-13"
    assert [v["trigger"] for v in day["versions"]] == ["pipeline", "preset"]
    assert day["versions"][1]["conclusion"]["effective_cap_pct"] == 20
    assert all(v["recorded_at"] for v in day["versions"])


def test_append_dedupes_identical_conclusion(tmp_path):
    assert archive.append_version(tmp_path, _conclusion(), "pipeline")
    # Evidence refresh alone is not a new decision.
    assert not archive.append_version(tmp_path, _conclusion(evidence_as_of="2026-11-16T10:00:00+08:00"), "preset")
    assert len(archive.load_day(tmp_path, "2026-11-13")["versions"]) == 1


def test_no_date_is_not_archived(tmp_path):
    assert not archive.append_version(tmp_path, _conclusion(date=None), "pipeline")
    assert not (tmp_path / "user_data" / archive.ARCHIVE_DIR).exists()


@pytest.mark.parametrize("content", ["{not json", json.dumps({"versions": "x"}), json.dumps([1])])
def test_append_skips_corrupt_archive(tmp_path, content):
    path = _path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    assert not archive.append_version(tmp_path, _conclusion(), "pipeline")
    assert path.read_text(encoding="utf-8") == content
    assert archive.load_day(tmp_path, "2026-11-13") is None


def test_rejects_unsafe_day(tmp_path):
    assert not archive.append_version(tmp_path, _conclusion(date="../evil"), "pipeline")
    assert archive.load_day(tmp_path, "../evil") is None


def test_build_conclusion_reports_archive_summary(tmp_path):
    from datetime import date

    import polars as pl

    from app.services import regime_builder

    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 11, 13)], "state": ["weak"], "score": [30], "capital_volume": ["flat"]}))
    plan = {"weak_flat": {"cap_pct": 10, "styles": ["空仓观望"], "note": ""}}
    assert ec.build_conclusion(tmp_path, plan=plan)["archive"] is None
    archive.append_version(tmp_path, ec.build_conclusion(tmp_path, plan=plan), "pipeline")
    summary = ec.build_conclusion(tmp_path, plan=plan)["archive"]
    assert summary["versions"] == 1 and summary["latest_effective_cap_pct"] == 10
    assert summary["latest_at"]


def test_saving_preset_appends_version(tmp_path, monkeypatch):
    from app.api import settings as settings_api
    from app.services import preferences

    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: path)
    preferences._invalidate_cache()
    monkeypatch.setattr(settings_api, "_conclusion_data_dir", lambda: tmp_path)
    captured = []
    monkeypatch.setattr(ec, "build_conclusion", lambda data_dir, plan=None: _conclusion(plan=(plan or {}).get("weak_flat")))
    monkeypatch.setattr(archive, "append_version", lambda d, c, trigger: captured.append((c, trigger)) or True)
    settings_api.update_env_matrix_plan(settings_api.EnvMatrixPlanIn(
        plan={"weak_flat": {"cap_pct": 5, "styles": [], "note": ""}}))
    assert captured and captured[0][1] == "preset"
    assert captured[0][0]["plan"]["cap_pct"] == 5
    preferences._invalidate_cache()


def test_preset_archive_failure_does_not_break_save(tmp_path, monkeypatch):
    from app.api import settings as settings_api
    from app.services import preferences

    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: path)
    preferences._invalidate_cache()
    monkeypatch.setattr(settings_api, "_conclusion_data_dir", lambda: tmp_path)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ec, "build_conclusion", boom)
    out = settings_api.update_env_matrix_plan(settings_api.EnvMatrixPlanIn(
        plan={"weak_flat": {"cap_pct": 5, "styles": [], "note": ""}}))
    assert out["plan"]["weak_flat"]["cap_pct"] == 5
    preferences._invalidate_cache()
