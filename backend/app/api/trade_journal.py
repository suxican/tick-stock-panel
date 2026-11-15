"""交易记录 API — 结清批次留下的记录: 列表 + 累计统计 + 删除。

写入入口在批次 API 的结清端点 (POST /api/lots/{lot_id}/close); 这里只读与纠错删除。
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request

from app.strategy import trade_journal

router = APIRouter(prefix="/api/trade-journal", tags=["trade-journal"])

_ENTRY_ID_RE = re.compile(r"^tj_[a-z0-9_]{1,40}$")


def with_pnl(entry: dict) -> dict:
    """附加读取时计算的盈亏百分数 (不落盘)。"""
    return {**entry, "pnl_pct": trade_journal.pnl_pct(entry)}


@router.get("")
def list_entries(request: Request):
    """全部记录 (卖出日倒序) 与累计统计。"""
    entries = trade_journal.load_all(request.app.state.repo.store.data_dir)
    entries.sort(key=lambda e: (e.get("sell_date") or "", e.get("created_at") or ""), reverse=True)
    return {"entries": [with_pnl(e) for e in entries], "stats": trade_journal.stats(entries)}


@router.delete("/{entry_id}")
def delete_entry(entry_id: str, request: Request):
    """删除一条记录 (纠正误操作); 不恢复批次。"""
    if not _ENTRY_ID_RE.match(entry_id):
        raise HTTPException(status_code=400, detail="交易记录 id 非法")
    return {"ok": trade_journal.delete_one(request.app.state.repo.store.data_dir, entry_id)}
