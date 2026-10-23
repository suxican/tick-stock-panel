"""Signal-to-trade attribution from the existing paper ledger, without execution."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from app.market_time import CN_TZ, cn_today
from app.services.first_board_ledger import (
    LedgerError,
    LedgerUnavailableError,
    number,
    position_matches,
    project_fifo,
    read_ledger_snapshot,
    trading_date,
)

DETAIL_LIMIT = 500
MAX_EVENTS = 100_000
_LIMITATIONS = [
    "窗口按买入信号日期选择；先校验完整账户账本，再仅按截至日及以前的经济成交日期重建 FIFO。",
    "无已创建委托不能区分未尝试下单与创建前被拒；未委托、未成交不记零收益。",
    "全账户 FIFO 决定卖出归属，手工或其他来源卖出不会被标为已验证的首板退出。",
    "成交价已含纸盘实际滑点，不重复扣减；历史缺费用记录不计入净收益和闭环胜率。",
    "未平仓不计闭环胜率；部分平仓只统计已经实现的净收益，不推算未实现盈亏。",
    "历史取消或过期委托缺少终结时间时，无法还原截至日状态，按证据不足处理。",
    "沿用纸盘公司行动数量调整与账本交易日 T+1；不包含账本未记录的现金分红，不证明实盘可成交。",
]


def _timestamp(value: object) -> datetime:
    try:
        if not isinstance(value, str):
            raise ValueError
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(CN_TZ)
    except (TypeError, ValueError, OverflowError) as exc:
        raise LedgerError("委托或成交时间缺失、无时区或无效") from exc


def _signal_time(event: dict) -> datetime:
    try:
        result = datetime.fromtimestamp(number(event.get("ts"), positive=True) / 1000, CN_TZ)
    except (ValueError, OverflowError, OSError) as exc:
        raise LedgerError("首板信号时间无效") from exc
    if result.date().isoformat() != trading_date(event.get("date")):
        raise LedgerError("首板信号日期与时间不一致")
    return result


def _blank_row(event: dict) -> dict:
    return {"signal_id": event["id"], "symbol": event["symbol"],
            "signal_at": _signal_time(event).isoformat(), "revision": event["revision"],
            "status": "evidence_missing", "reason": "交易证据尚未验证", "orders": [],
            "fills": [], "exits": [], "remaining_qty": None,
            "realized_net_pnl": None, "closed_net_return": None}


def _empty_summary() -> dict:
    return {"submitted_signals": 0, "filled_signals": 0, "closed_signals": 0,
            "open_signals": 0, "partially_closed_signals": 0, "unfilled_signals": 0,
            "unsubmitted_signals": 0, "unknown_signals": 0, "realized_net_pnl": None,
            "closed_net_pnl": None, "closed_win_rate": None, "closed_mean_net_return": None}


def _order_view(order: dict) -> dict:
    return {key: order.get(key) for key in (
        "id", "symbol", "side", "status", "qty", "source", "created_at", "filled_at", "ref_price", "reason",
    )}


def _verify_ledger(snapshot: dict) -> tuple[dict, dict]:
    orders = {}
    for order in snapshot["orders"]:
        identifier = order.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in orders:
            raise LedgerError("委托标识缺失或重复")
        if not isinstance(order.get("symbol"), str) or not order["symbol"]:
            raise LedgerError("委托缺少股票标识")
        if order.get("side") not in {"buy", "sell"} or order.get("status") not in {
            "pending", "filled", "cancelled", "expired",
        }:
            raise LedgerError("委托方向或状态不受支持")
        if number(order.get("qty"), positive=True) % 1:
            raise LedgerError("委托数量无效")
        if not isinstance(order.get("source"), str) or not order["source"]:
            raise LedgerError("委托来源缺失，不能验证交易归属")
        _timestamp(order.get("created_at"))
        orders[identifier] = order
    projected = project_fifo(snapshot["fills"], enforce_t1=True)
    fill_qty = defaultdict(float)
    fill_count = defaultdict(int)
    last_times = {}
    for fill in projected["fills"]:
        order = orders.get(fill["order_id"])
        if order is None:
            raise LedgerError("成交记录缺少对应委托，不能验证来源")
        if order["symbol"] != fill["symbol"] or order["side"] != fill["side"]:
            raise LedgerError("成交与委托的股票或方向不一致")
        filled_at = _timestamp(fill["ts"])
        created_at = _timestamp(order["created_at"])
        # Historical day settlement can be recorded later than its economic date.
        if (filled_at < created_at or fill["date"] > filled_at.date().isoformat()
                or fill["date"] < created_at.date().isoformat()):
            raise LedgerError("成交时间早于委托或交易日期晚于记录时间")
        previous = last_times.get(fill["symbol"])
        if previous is not None and filled_at < previous:
            raise LedgerError("成交台账记录时间顺序无效")
        last_times[fill["symbol"]] = filled_at
        if order.get("filled_at") is not None and _timestamp(order["filled_at"]) != filled_at:
            raise LedgerError("委托与成交的完成时间不一致")
        fill_qty[order["id"]] += fill["qty"]
        fill_count[order["id"]] += 1
    for identifier, order in orders.items():
        expected = number(order["qty"]) if order["status"] == "filled" else 0
        if fill_qty[identifier] != expected or fill_count[identifier] > 1:
            raise LedgerError("委托状态、数量与成交台账不一致")
    positions = snapshot["positions"]
    if positions is not None:
        for symbol in positions.keys() | projected["aggregate_qty"].keys():
            total = projected["aggregate_qty"].get(symbol, 0)
            position = positions.get(symbol)
            if position is None and total == 0:
                continue
            if not position_matches(projected["lots"].get(symbol, []), total, position):
                raise LedgerError("FIFO 重放数量与已保存持仓不一致")
    return orders, projected


def _attribute_row(row: dict, event: dict, events_by_id: dict, orders: dict,
                   entry_orders: list[dict], fills_by_order: dict, entries_by_order: dict,
                   matches_by_order: dict) -> None:
    row["orders"] = [_order_view(order) for order in entry_orders]
    if not entry_orders:
        row.update(status="not_ordered", reason="无已创建委托；历史记录不能区分未尝试与创建前被拒")
        return
    if len(entry_orders) != 1:
        row["reason"] = "同一买入信号存在多张买单，来源证据不唯一"
        return
    order = entry_orders[0]
    signal_at = _signal_time(event)
    if (order["symbol"] != row["symbol"] or order["side"] != "buy"
            or _timestamp(order["created_at"]) < signal_at):
        row["reason"] = "信号股票或时间与来源委托不一致"
        return
    fills = fills_by_order[order["id"]]
    row["fills"] = fills
    if order.get("historical_termination_unknown"):
        row["reason"] = "当前委托已取消或过期，但缺少终结时间，不能确认历史截至日状态"
        return
    if not fills:
        state = order["status"]
        row.update(status=state, reason={"pending": "委托尚未成交", "cancelled": "委托已取消",
                                        "expired": "委托已过期或被撮合拒绝"}[state])
        return
    if any(fill["date"] < event["date"] or _timestamp(fill["ts"]) < signal_at for fill in fills):
        row["reason"] = "买入成交早于来源信号"
        return
    entries = entries_by_order[order["id"]]
    matches = matches_by_order[order["id"]]
    missing_fee = any(fill["fee_defaulted"] for fill in fills)
    exit_orders = {}
    for match in matches:
        sell = orders[match["sell_order_id"]]
        source = sell["source"]
        exit_id = source.removeprefix("first_board:") if source.startswith("first_board:") else None
        exit_signal = events_by_id.get(exit_id)
        verified = bool(exit_signal and exit_signal.get("event_type") == "exit_candidate"
                        and exit_signal.get("symbol") == row["symbol"]
                        and _signal_time(exit_signal) <= _timestamp(sell["created_at"]))
        row["exits"].append({**match, "source": source, "manual_exit": source == "manual",
                             "exit_signal_id": exit_id, "exit_signal_verified": verified})
        exit_orders[sell["id"]] = sell
        missing_fee |= any(fill["fee_defaulted"] for fill in fills_by_order[sell["id"]])
    row["orders"].extend(_order_view(order) for order in exit_orders.values())
    row["remaining_qty"] = sum(entry["qty"] for entry in entries)
    if missing_fee:
        row["reason"] = "历史买入或退出成交缺少实际费用，成本证据不完整；兼容重放不计入净收益"
        # The FIFO slices remain traceable but their apparent net results are not certified.
        for exit_row in row["exits"]:
            exit_row["net_pnl"] = None
        return
    row["realized_net_pnl"] = sum(match["net_pnl"] for match in matches)
    if row["remaining_qty"] <= 1e-9:
        row.update(status="closed", reason="买入批次已按全账户 FIFO 完整平仓",
                   closed_net_return=row["realized_net_pnl"] / sum(entry["entry_cost"] for entry in entries))
    elif matches:
        row.update(status="partially_closed", reason="部分批次已卖出，剩余持仓不计闭环胜率")
    else:
        row.update(status="open", reason="买入已成交，尚无已归属的退出成交")


def _summarize(rows: list[dict]) -> dict:
    result = _empty_summary()
    valid = [row for row in rows if row["status"] in {"open", "partially_closed", "closed"}]
    closed = [row for row in valid if row["status"] == "closed"]
    result.update(
        submitted_signals=sum(bool(row["orders"]) for row in rows),
        filled_signals=sum(bool(row["fills"]) for row in rows),
        closed_signals=len(closed), open_signals=sum(row["status"] == "open" for row in rows),
        partially_closed_signals=sum(row["status"] == "partially_closed" for row in rows),
        unfilled_signals=sum(row["status"] in {"pending", "cancelled", "expired"} for row in rows),
        unsubmitted_signals=sum(row["status"] == "not_ordered" for row in rows),
        unknown_signals=sum(row["status"] == "evidence_missing" for row in rows),
        realized_net_pnl=sum(row["realized_net_pnl"] for row in valid) if valid else None,
        closed_net_pnl=sum(row["realized_net_pnl"] for row in closed) if closed else None,
        closed_win_rate=sum(row["realized_net_pnl"] > 0 for row in closed) / len(closed) if closed else None,
        closed_mean_net_return=sum(row["closed_net_return"] for row in closed) / len(closed) if closed else None,
    )
    return result


def build_paper_trade_audit(
    data_dir: Path, account_id: str | None, *, events: list[dict], start: date, end: date,
    revision: int | None = None,
) -> dict:
    """Bounded, read-only cohort audit. Ratios are fractions; unknown PnL is null."""
    result = {"status": "error", "reason": "交易证据不可用", "account_id": account_id,
              "coverage": {"signal_count": 0, "displayed_count": 0, "detail_limit": DETAIL_LIMIT,
                           "ledger_order_count": 0, "ledger_fill_count": 0, "attributable_count": 0,
                           "unknown_count": 0, "legacy_fee_default_count": 0, "truncated": False},
              "summary": _empty_summary(), "trades": [], "limitations": list(_LIMITATIONS), "fingerprint": None}
    rows = []
    try:
        as_of = cn_today()
        if (type(start) is not date or type(end) is not date or start > end or end > as_of
                or len(events) > MAX_EVENTS
                or (revision is not None and (type(revision) is not int or revision < 0))):
            raise LedgerError("信号查询范围无效或超过审计上限")
        events_by_id, selected = {}, []
        for event in events:
            if not isinstance(event, dict) or not isinstance(event.get("id"), str) or not event["id"]:
                raise LedgerError("首板事件标识缺失")
            if event["id"] in events_by_id:
                raise LedgerError("首板事件标识重复")
            events_by_id[event["id"]] = event
            if event.get("event_type") != "buy_candidate":
                continue
            day = trading_date(event.get("date"))
            if not start.isoformat() <= day <= end.isoformat() or (revision is not None and event.get("revision") != revision):
                continue
            if (not isinstance(event.get("symbol"), str) or not event["symbol"]
                    or type(event.get("revision")) is not int or event["revision"] < 0):
                raise LedgerError("买入信号股票或规则版本无效")
            selected.append(event)
        selected.sort(key=lambda event: (_signal_time(event), event["id"]), reverse=True)
        rows = [_blank_row(event) for event in selected]
        result["coverage"].update(signal_count=len(rows), displayed_count=min(len(rows), DETAIL_LIMIT),
                                  truncated=len(rows) > DETAIL_LIMIT)
        if not account_id:
            raise LedgerUnavailableError("未选择或关联模拟账户")
        snapshot = read_ledger_snapshot(data_dir, account_id)
        result["coverage"].update(ledger_order_count=len(snapshot["orders"]), ledger_fill_count=len(snapshot["fills"]))
        result["fingerprint"] = hashlib.sha256(json.dumps(
            {"version": 1, "ledger": snapshot["fingerprint"], "events": events,
             "start": start.isoformat(), "end": end.isoformat(), "revision": revision, "as_of": as_of.isoformat()},
            ensure_ascii=False, sort_keys=True, allow_nan=False,
        ).encode()).hexdigest()
        orders, projected = _verify_ledger(snapshot)
        result["coverage"]["legacy_fee_default_count"] = projected["legacy_fee_default_count"]
        cutoff = end.isoformat()
        through_fills = [fill for fill in snapshot["fills"] if fill["date"] <= cutoff]
        if len(through_fills) != len(snapshot["fills"]):
            projected = project_fifo(through_fills, enforce_t1=True)
        included_orders = {fill["order_id"] for fill in projected["fills"]}
        orders = {identifier: {**order} for identifier, order in orders.items()
                  if _timestamp(order["created_at"]).date() <= end}
        for identifier, order in orders.items():
            if order["status"] == "filled" and identifier not in included_orders:
                order.update(status="pending", filled_at=None)
            elif order["status"] in {"cancelled", "expired"} and end < as_of:
                order["historical_termination_unknown"] = True
        orders_by_source, fills_by_order, entries_by_order, matches_by_order = (defaultdict(list) for _ in range(4))
        for order in orders.values():
            orders_by_source[order["source"]].append(order)
        for fill in projected["fills"]:
            fills_by_order[fill["order_id"]].append(fill)
        for entry in projected["entries"]:
            entries_by_order[entry["order_id"]].append(entry)
        for match in projected["matches"]:
            matches_by_order[match["buy_order_id"]].append(match)
        for row, event in zip(rows, selected, strict=True):
            _attribute_row(row, event, events_by_id, orders, orders_by_source[f"first_board:{event['id']}"],
                           fills_by_order, entries_by_order, matches_by_order)
        result["summary"] = _summarize(rows)
        unknown = result["summary"]["unknown_signals"]
        result["coverage"].update(attributable_count=sum(row["status"] in {"open", "partially_closed", "closed"} for row in rows),
                                  unknown_count=unknown)
        result.update(status="incomplete" if unknown else "ready",
                      reason="部分信号证据不足，相关收益不计入统计" if unknown else "已完成信号与现有模拟账本的只读归因")
    except (LedgerError, TypeError, ValueError, OverflowError) as exc:
        result.update(status="unavailable" if isinstance(exc, LedgerUnavailableError) else "error",
                      reason=str(exc) if isinstance(exc, LedgerError) else "交易证据格式无效，不能可靠归因")
        for row in rows:
            row.update(status="evidence_missing", reason=result["reason"], remaining_qty=None,
                       realized_net_pnl=None, closed_net_return=None)
            for exit_row in row["exits"]:
                exit_row["net_pnl"] = None
        result["summary"] = _empty_summary()
        result["summary"]["unknown_signals"] = len(rows)
        result["coverage"]["unknown_count"] = len(rows)
    result["trades"] = rows[:DETAIL_LIMIT]
    return result
