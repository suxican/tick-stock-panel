"""环境结论: 定位当天格子、叠加博弈页闸门、附每格历史证据。"""
from __future__ import annotations

import json
import os
from datetime import date, datetime

import polars as pl
import pytest

from app.services import environment_conclusion as ec
from app.services import regime_builder

DAY = date(2026, 11, 13)
PLAN = {"strong_expand": {"cap_pct": 70, "styles": ["打板"], "note": "龙头"}}


def _regime(tmp_path, *, state="strong", volume="expand", day=DAY):
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date(2026, 11, 12), day], "state": ["weak", state], "score": [30, 60],
        "capital_volume": ["shrink", volume],
    }, schema_overrides={"state": pl.Utf8, "capital_volume": pl.Utf8}))


def _report(tmp_path, *, as_of=DAY, total_cap=0.5, created="2026-11-13T16:00:00+08:00",
            mtime=None, name="0" * 32, payload=None, total_cap_only=False):
    root = tmp_path / "user_data" / "market_game"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"mg_{name}.json"
    report = {"id": name, "as_of": as_of.isoformat(), "created_at": created,
              "allocation": {"total_cap": 0.0} if total_cap_only else {"total_cap": 0.0, "market_gate": total_cap}}
    path.write_text(json.dumps(payload if payload is not None else {"report": report, "snapshot": {}}), encoding="utf-8")
    if mtime is not None:
        stamp = mtime.timestamp()
        os.utime(path, (stamp, stamp))
    return path


def _archive(tmp_path, cells):
    root = tmp_path / "user_data" / "market_game" / "calibration"
    root.mkdir(parents=True, exist_ok=True)
    (root / "cal_20261116T081244_4da4e088.json").write_text(json.dumps(
        {"schema_version": 1, "status": "ready", "created_at": "2026-11-16T08:12:44+08:00", "rows": [], "cells": cells}),
        encoding="utf-8")


@pytest.mark.parametrize("state,volume,cell", [
    ("strong", "expand", "strong_expand"), ("lean_strong", "flat", "strong_flat"),
    ("range", "shrink", "range_shrink"), ("lean_weak", "expand", "weak_expand"), ("weak", "flat", "weak_flat"),
])
def test_locates_cell(tmp_path, state, volume, cell):
    _regime(tmp_path, state=state, volume=volume)
    out = ec.build_conclusion(tmp_path, plan={})
    assert (out["date"], out["cell"], out["missing_reason"]) == ("2026-11-13", cell, None)


@pytest.mark.parametrize("state,volume,reason", [
    (None, "expand", "环境分缺失"), ("strong", None, "资金档位未计算"), ("mystery", "expand", "环境分缺失"),
])
def test_missing_cell_reason(tmp_path, state, volume, reason):
    _regime(tmp_path, state=state, volume=volume)
    out = ec.build_conclusion(tmp_path, plan=PLAN)
    assert out["cell"] is None and out["missing_reason"] == reason
    assert out["plan"] is None and out["effective_cap_pct"] is None


def test_empty_history(tmp_path):
    out = ec.build_conclusion(tmp_path, plan=PLAN)
    assert out["date"] is None and out["missing_reason"] == "暂无环境历史"


def test_plan_capped_by_gate(tmp_path):
    _regime(tmp_path)
    _report(tmp_path, total_cap=0.5)
    out = ec.build_conclusion(tmp_path, plan=PLAN)
    assert out["plan"] == PLAN["strong_expand"]
    assert out["gate"]["cap_pct"] == 50 and out["gate"]["as_of"] == "2026-11-13"
    assert (out["effective_cap_pct"], out["limited_by"]) == (50, "gate")


@pytest.mark.parametrize("total_cap,effective,limited", [(0.8, 70, "plan"), (0.7, 70, "both")])
def test_plan_vs_gate(tmp_path, total_cap, effective, limited):
    _regime(tmp_path)
    _report(tmp_path, total_cap=total_cap)
    out = ec.build_conclusion(tmp_path, plan=PLAN)
    assert (out["effective_cap_pct"], out["limited_by"]) == (effective, limited)


def test_plan_without_gate(tmp_path):
    _regime(tmp_path)
    out = ec.build_conclusion(tmp_path, plan=PLAN)
    assert out["gate"] is None
    assert (out["effective_cap_pct"], out["limited_by"]) == (70, "plan")


def test_gate_without_plan_has_no_effective_cap(tmp_path):
    _regime(tmp_path)
    _report(tmp_path, total_cap=0.3)
    out = ec.build_conclusion(tmp_path, plan={})
    assert out["plan"] is None and out["gate"]["cap_pct"] == 30
    assert out["effective_cap_pct"] is None and out["limited_by"] is None


def test_gate_only_same_day_report(tmp_path):
    _regime(tmp_path)
    _report(tmp_path, as_of=date(2026, 11, 12), total_cap=0.1)
    assert ec.build_conclusion(tmp_path, plan=PLAN)["gate"] is None


def test_gate_picks_latest_same_day_report(tmp_path):
    _regime(tmp_path)
    _report(tmp_path, total_cap=0.2, created="2026-11-13T16:00:00+08:00", name="1" * 32)
    _report(tmp_path, total_cap=0.4, created="2026-11-13T20:00:00+08:00", name="2" * 32)
    gate = ec.build_conclusion(tmp_path, plan=PLAN)["gate"]
    assert gate["cap_pct"] == 40 and gate["report_id"] == "2" * 32


def test_gate_skips_reports_older_than_date(tmp_path):
    _regime(tmp_path)
    # 同日报告但文件 mtime 早于环境日期: 不会被读取(列表热路径只读近期文件)
    _report(tmp_path, total_cap=0.2, mtime=datetime(2026, 11, 1, 12, 0))
    assert ec.build_conclusion(tmp_path, plan=PLAN)["gate"] is None


def test_gate_ignores_broken_report(tmp_path):
    _regime(tmp_path)
    _report(tmp_path, name="1" * 32, payload={"report": {"as_of": "2026-11-13", "created_at": "2026-11-13T21:00:00+08:00"}})
    path = _report(tmp_path, name="3" * 32)
    path.with_name("mg_" + "4" * 32 + ".json").write_text("{not json", encoding="utf-8")
    gate = ec.build_conclusion(tmp_path, plan=PLAN)["gate"]
    assert gate["report_id"] == "3" * 32


def test_evidence_from_latest_archive(tmp_path):
    _regime(tmp_path)
    stats = {"all": {"n": 238, "mean": .0036, "positive_rate": .58, "p10": -.012},
             "train": {"n": 91, "mean": .0044, "positive_rate": .6, "p10": -.011},
             "test": {"n": 147, "mean": .0031, "positive_rate": .57, "p10": -.013}}
    _archive(tmp_path, {"strong_expand": stats})
    out = ec.build_conclusion(tmp_path, plan=PLAN)
    assert out["evidence"] == stats
    assert out["evidence_as_of"] == "2026-11-16T08:12:44+08:00"


def test_evidence_missing_without_archive_or_cells(tmp_path):
    _regime(tmp_path)
    assert ec.build_conclusion(tmp_path, plan=PLAN)["evidence"] is None
    _archive(tmp_path, None)
    assert ec.build_conclusion(tmp_path, plan=PLAN)["evidence"] is None


def test_reads_plan_from_preferences_when_not_given(tmp_path, monkeypatch):
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_env_matrix_plan", lambda: PLAN)
    _regime(tmp_path)
    assert ec.build_conclusion(tmp_path)["plan"] == PLAN["strong_expand"]


def test_api_returns_conclusion_for_empty_data_dir(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.api import regime as regime_api
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_env_matrix_plan", lambda: {})
    monkeypatch.setattr(regime_api, "_data_dir", lambda request: tmp_path)
    out = regime_api.regime_conclusion(SimpleNamespace())
    assert out["date"] is None and out["missing_reason"] == "暂无环境历史"



def test_old_report_without_market_gate_is_unknown_not_zero(tmp_path):
    _regime(tmp_path)
    _report(tmp_path, total_cap_only=True)
    out = ec.build_conclusion(tmp_path, plan=PLAN)
    assert out["gate"]["cap_pct"] is None and "旧版" in out["gate"]["note"]
    assert (out["effective_cap_pct"], out["limited_by"]) == (70, "plan")


def test_gate_ignores_entry_pattern_total_cap(tmp_path):
    # total_cap is zero on days without the market-game entry pattern; the risk gate is not.
    _regime(tmp_path)
    _report(tmp_path, total_cap=1.0)
    out = ec.build_conclusion(tmp_path, plan=PLAN)
    assert out["gate"]["cap_pct"] == 100
    assert (out["effective_cap_pct"], out["limited_by"]) == (70, "plan")
