"""批次登记 API — 薄"批次"页 (持仓提醒), 只做胶水, 不含会计语义。

映射/校验/持久化在 strategy.lots 域; 写完派生规则后复用 monitor_rules 的 _sync_engine 同步引擎。
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.strategy import lots as lots_domain
from app.strategy import monitor_rules

router = APIRouter(prefix="/api/lots", tags=["lots"])

# 批次 + 派生规则 + 引擎重载的跨请求互斥; 规则全部校验通过才落盘, 避免半成品 (镜像 watchlist 服务层)。
_write_lock = threading.Lock()


def _data_dir(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _resolve_asset_type(request: Request, symbol: str) -> str:
    """按 symbol 解析资产类型 (stock/etf); 解析失败默认 stock (fail-safe)。"""
    repo = getattr(request.app.state, "repo", None)
    try:
        return repo.resolve_asset_type(symbol) if repo is not None else "stock"
    except Exception:
        # 回退为 stock 会让 etf 批次的止盈止损规则落入错误的监控轮, 必须留痕排查
        logging.getLogger(__name__).warning(
            "resolve_asset_type failed for %s, falling back to stock", symbol, exc_info=True
        )
        return "stock"


class LotModel(BaseModel):
    id: str | None = None
    symbol: str
    qty: float = 0
    cost_price: float = 0
    buy_date: str | None = None
    target_pct: float = 0
    stop_pct: float = 0
    remind_date: str | None = None
    lead_days: int = 1


def _reload_engine(request: Request) -> None:
    """批次规则保存/删除后重载引擎 — 复用监控规则 API 的共享重载 (含指数纠正)。"""
    from app.api.monitor_rules import _sync_engine

    _sync_engine(request)


def sync_lot(request: Request, lot: dict) -> None:
    """写批次文件 + 同步其两条派生监控规则 + 重载引擎。

    派生规则继承用户默认推送渠道 (webhook_default_channels), 否则批次告警会静默只走应用内。
    """
    from app.services import preferences

    data_dir = _data_dir(request)
    with _write_lock:
        default_channels = preferences.get_webhook_default_channels()
        # ETF/指数等资产类型解析 (止盈止损价格规则须走对应资产监控轮才会触发)
        asset_type = _resolve_asset_type(request, lot["symbol"])
        price_rule, date_rule = lots_domain.lot_to_rules(lot)
        rules_to_write: list[dict] = []
        rules_to_delete: list[str] = []
        for rid, rule in ((f"{lot['id']}_p", price_rule), (f"{lot['id']}_d", date_rule)):
            if rule is None:
                rules_to_delete.append(rid)
                continue
            rule["asset_type"] = asset_type
            rule.setdefault("webhook_channels", list(default_channels))
            # 保留旧 created_at, 避免编辑批次后派生规则在监控中心列表跳位
            existing = monitor_rules.load_one(data_dir, rid)
            if existing and existing.get("created_at"):
                rule["created_at"] = existing["created_at"]
            try:
                monitor_rules.validate(rule)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            rules_to_write.append(monitor_rules.normalize(rule))
        lots_domain.save_one(data_dir, lot)
        for rid in rules_to_delete:
            monitor_rules.delete_one(data_dir, rid)
        for rule in rules_to_write:
            monitor_rules.save_one(data_dir, rule)
    _reload_engine(request)


@router.get("")
def list_lots(request: Request):
    return {"lots": lots_domain.load_all(_data_dir(request))}


@router.post("")
def upsert_lot(lot_in: LotModel, request: Request):
    """新建/更新一个批次。id 缺省时服务端生成 (紧凑, 保证 {id}_p/_d 规则 id ≤ 40 字符)。"""
    lot = lot_in.model_dump()
    if not lot.get("id"):
        lot["id"] = f"lot_{int(time.time() * 1000):x}_{secrets.token_hex(2)}"
    try:
        lots_domain.validate_lot(lot)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    lot = lots_domain.normalize_lot(lot)
    sync_lot(request, lot)
    return {"ok": True, "lot": lot}


def _delete_locked(data_dir: Path, lot_id: str) -> bool:
    """删批次文件与两条派生规则 (调用方须持有 _write_lock); 返回是否有任何文件被删。"""
    deleted = lots_domain.delete_one(data_dir, lot_id)
    # 两条派生规则都要删 (用 or 会短路跳过第二条)
    deleted_p = monitor_rules.delete_one(data_dir, f"{lot_id}_p")
    deleted_d = monitor_rules.delete_one(data_dir, f"{lot_id}_d")
    return deleted or deleted_p or deleted_d


@router.delete("/{lot_id}")
def delete_lot(lot_id: str, request: Request):
    if not monitor_rules.ID_RE.match(lot_id):
        raise HTTPException(status_code=400, detail="批次 id 非法")
    data_dir = _data_dir(request)
    with _write_lock:
        deleted = _delete_locked(data_dir, lot_id)
    if deleted:
        _reload_engine(request)
    return {"ok": True}


class CloseLotModel(BaseModel):
    sell_price: float
    sell_date: str
    reason: str
    note: str = ""


def _find_lot(data_dir: Path, lot_id: str) -> dict | None:
    return next((lot for lot in lots_domain.load_all(data_dir) if lot.get("id") == lot_id), None)


@router.post("/{lot_id}/close")
def close_lot(lot_id: str, body: CloseLotModel, request: Request):
    """结清批次: 留下一条交易记录, 再按删除逻辑移除批次与派生规则。

    与"删除"的区别只在留痕; 批次本身仍不做会计 (记录在 strategy.trade_journal)。
    """
    from app.api.trade_journal import with_pnl
    from app.market_time import cn_today
    from app.services import trade_journal_context
    from app.services.holding_analysis import lots_invalidation_flags
    from app.strategy import trade_journal

    if not monitor_rules.ID_RE.match(lot_id):
        raise HTTPException(status_code=400, detail="批次 id 非法")
    data_dir = _data_dir(request)
    lot = _find_lot(data_dir, lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="批次不存在或已结清")
    try:
        trade_journal.validate_close(
            lot, body.sell_price, body.sell_date, body.reason, body.note, today=cn_today(),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # 上下文在锁外取 (日历/阶段历史是磁盘读取), 锁内只做文件写删
    symbol = lot["symbol"]
    try:
        name = request.app.state.repo.get_name_map([symbol]).get(symbol) or symbol
    except Exception:  # noqa: BLE001 - 名称只用于展示, 取不到退回代码
        name = symbol
    entry = trade_journal.build_entry(
        lot, sell_price=body.sell_price, sell_date=body.sell_date, reason=body.reason, note=body.note,
        name=name,
        had_invalidation=lots_invalidation_flags(data_dir, [symbol])[symbol],
        buy_phase=trade_journal_context.buy_phase(data_dir, lot.get("buy_date")),
        hold_days=trade_journal_context.hold_days(lot.get("buy_date"), body.sell_date),
    )
    with _write_lock:
        # 并发的另一次结清/删除可能已先完成: 批次不在就不再写记录
        if _find_lot(data_dir, lot_id) is None:
            raise HTTPException(status_code=404, detail="批次不存在或已结清")
        trade_journal.save_one(data_dir, entry)
        _delete_locked(data_dir, lot_id)
    _reload_engine(request)
    return {"ok": True, "entry": with_pnl(entry)}
