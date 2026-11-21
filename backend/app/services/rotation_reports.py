"""AI 维度轮动分析报告(概念 / 行业)持久化存储。

与 ai_reports / stock_reports 完全独立: 单独文件、字段、上限。存储机制委托
JsonReportStore(原子写 + 实例锁)。

存储位置: data/user_data/ai_rotation_reports.json (数组, 按 created_at 降序)
保留最近 MAX_REPORTS 条, 超出自动裁剪最旧的。

每条报告结构:
{
  "id": "rot_xxx",
  "kind": "concept" | "industry",
  "level": 2 | null,            # 行业层级, 概念为 null
  "days": 12,                   # 分析最近 N 个交易日
  "focus": "",                  # 用户追加的关注点
  "content": "# ...markdown",   # 报告正文
  "summary": "...",             # 轮动信号摘要(meta 事件)
  "created_at": "2026-11-20T10:00:00"
}
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from app.services.json_report_store import JsonReportStore

logger = logging.getLogger(__name__)

MAX_REPORTS = 50

_store = JsonReportStore("ai_rotation_reports.json", MAX_REPORTS, id_prefix="rot", id_with_symbol=False)


def list_reports(kind: str | None = None, level: int | None = None) -> list[dict]:
    """返回报告(按 created_at 降序); kind / level 非空时过滤。"""
    reports = _store.list_reports()
    if kind:
        reports = [r for r in reports if r.get("kind") == kind]
    if level is not None:
        reports = [r for r in reports if r.get("level") == level]
    return reports


def save_report(report: dict) -> dict:
    return _store.save_report(report)


def delete_report(report_id: str) -> bool:
    return _store.delete_report(report_id)


async def persist_stream(
    events: AsyncIterator[str],
    *,
    kind: str,
    level: int | None,
    days: int,
    focus: str,
) -> AsyncIterator[str]:
    """透传 NDJSON 事件流, 同时累积正文; 流以 done 正常结束且无 error 时自动落盘。

    客户端中途断开(生成器被取消)或出现 error 事件均不保存。
    """
    parts: list[str] = []
    summary = ""
    failed = False
    done = False
    async for chunk in events:
        try:
            ev = json.loads(chunk)
        except ValueError:
            ev = {}
        etype = ev.get("type")
        if etype == "delta":
            parts.append(ev.get("content") or "")
        elif etype == "meta":
            summary = ev.get("summary") or ""
        elif etype == "error":
            failed = True
        elif etype == "done":
            done = True
        yield chunk

    content = "".join(parts).strip()
    if done and not failed and content:
        try:
            save_report({
                "kind": kind, "level": level, "days": days, "focus": focus,
                "content": content, "summary": summary,
            })
        except Exception:  # noqa: BLE001  落盘失败不影响已返回给用户的报告
            logger.warning("rotation report save failed", exc_info=True)
