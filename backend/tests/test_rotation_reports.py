"""AI 轮动分析报告历史: 流结束自动落盘 / 失败不保存 / 按维度过滤。"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.services import rotation_reports


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(rotation_reports._store, "_path", lambda: tmp_path / "ai_rotation_reports.json")


def _stream(*events: dict):
    async def gen():
        for e in events:
            yield json.dumps(e, ensure_ascii=False)
    return gen()


def _run(events, **kw):
    async def go():
        return [c async for c in rotation_reports.persist_stream(events, **kw)]
    return asyncio.run(go())


def test_done_stream_is_saved_and_passthrough():
    out = _run(
        _stream(
            {"type": "meta", "days": 12, "summary": "主线: 光刻机"},
            {"type": "delta", "content": "# 报告"},
            {"type": "delta", "content": "\n正文"},
            {"type": "done"},
        ),
        kind="industry", level=2, days=12, focus="关注光刻",
    )
    assert len(out) == 4  # 事件原样透传
    reports = rotation_reports.list_reports()
    assert len(reports) == 1
    r = reports[0]
    assert r["content"] == "# 报告\n正文"
    assert (r["kind"], r["level"], r["days"], r["focus"], r["summary"]) == (
        "industry", 2, 12, "关注光刻", "主线: 光刻机",
    )
    assert r["id"].startswith("rot_")


def test_error_stream_not_saved():
    _run(
        _stream({"type": "delta", "content": "半截"}, {"type": "error", "message": "boom"}),
        kind="concept", level=None, days=12, focus="",
    )
    assert rotation_reports.list_reports() == []


def test_stream_without_done_not_saved():
    _run(_stream({"type": "delta", "content": "半截"}), kind="concept", level=None, days=12, focus="")
    assert rotation_reports.list_reports() == []


def test_list_filters_by_kind_and_level_and_delete():
    for kind, level in [("concept", None), ("industry", 1), ("industry", 2)]:
        rotation_reports.save_report({"kind": kind, "level": level, "content": "x"})
    assert len(rotation_reports.list_reports("concept")) == 1
    assert len(rotation_reports.list_reports("industry")) == 2
    only = rotation_reports.list_reports("industry", 2)
    assert len(only) == 1
    assert rotation_reports.delete_report(only[0]["id"]) is True
    assert rotation_reports.list_reports("industry", 2) == []
