"""环境结论 9 格预设偏好 — 往返、清洗与接口。"""
from __future__ import annotations

import json

import pytest

from app.services import preferences


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: path)
    preferences._invalidate_cache()
    yield path
    preferences._invalidate_cache()


def test_cells_and_styles_are_fixed():
    assert preferences.ENV_MATRIX_CELLS == (
        "strong_expand", "strong_flat", "strong_shrink",
        "range_expand", "range_flat", "range_shrink",
        "weak_expand", "weak_flat", "weak_shrink")
    assert preferences.ENV_MATRIX_STYLES == ("打板", "半路", "低吸", "趋势持股", "空仓观望")


def test_plan_round_trip_and_default_empty():
    assert preferences.get_env_matrix_plan() == {}
    plan = {"strong_expand": {"cap_pct": 70, "styles": ["打板", "半路"], "note": "主线龙头"},
            "weak_shrink": {"cap_pct": 0, "styles": ["空仓观望"], "note": ""}}
    assert preferences.set_env_matrix_plan(plan) == plan
    assert preferences.get_env_matrix_plan() == plan


def test_set_plan_cleans_invalid_entries_and_replaces_whole_table():
    preferences.set_env_matrix_plan({"range_flat": {"cap_pct": 30, "styles": [], "note": ""}})
    saved = preferences.set_env_matrix_plan({
        "strong_expand": {"cap_pct": 60, "styles": ["打板", "追高", "打板"], "note": "x" * 80},
        "mystery_cell": {"cap_pct": 50, "styles": [], "note": ""},
        "weak_flat": {"cap_pct": 120, "styles": [], "note": ""},
        "weak_shrink": {"cap_pct": 12.5, "styles": [], "note": ""},
        "range_shrink": {"cap_pct": True, "styles": [], "note": ""},
        "strong_flat": "not a dict",
    })
    assert saved == {"strong_expand": {"cap_pct": 60, "styles": ["打板"], "note": "x" * 60}}
    assert "range_flat" not in preferences.get_env_matrix_plan()


def test_empty_plan_can_be_saved():
    preferences.set_env_matrix_plan({"strong_expand": {"cap_pct": 60, "styles": [], "note": ""}})
    assert preferences.set_env_matrix_plan({}) == {}
    assert preferences.get_env_matrix_plan() == {}


def test_get_plan_cleans_bad_stored_values(_isolated):
    _isolated.write_text(json.dumps({"env_matrix_plan": {
        "strong_expand": {"cap_pct": "70", "styles": "打板", "note": None},
        "weak_expand": {"cap_pct": 20, "styles": ["低吸"], "note": 5},
    }}), encoding="utf-8")
    preferences._invalidate_cache()
    assert preferences.get_env_matrix_plan() == {"weak_expand": {"cap_pct": 20, "styles": ["低吸"], "note": "5"}}
    _isolated.write_text(json.dumps({"env_matrix_plan": ["broken"]}), encoding="utf-8")
    preferences._invalidate_cache()
    assert preferences.get_env_matrix_plan() == {}


def test_api_put_then_get():
    from app.api.settings import EnvMatrixPlanIn, get_env_matrix_plan, update_env_matrix_plan

    body = EnvMatrixPlanIn(plan={"range_expand": {"cap_pct": 40, "styles": ["低吸"], "note": "轻仓"}})
    assert update_env_matrix_plan(body) == {"plan": {"range_expand": {"cap_pct": 40, "styles": ["低吸"], "note": "轻仓"}}}
    assert get_env_matrix_plan() == {"plan": {"range_expand": {"cap_pct": 40, "styles": ["低吸"], "note": "轻仓"}}}
