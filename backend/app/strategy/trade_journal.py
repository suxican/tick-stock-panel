"""交易记录域 — 批次结清时留下的卖出留痕与累计统计。

批次 (app/strategy/lots.py) 只生成监控规则、不做会计, 卖出后只能删除、不留痕。
"结清"把批次冻结成一条记录再删除批次, 记录在此存放; 批次模块语义不变。
只存价格, 盈亏在读取时计算 (pnl_pct 为百分数, 卖出价相对登记成本, 不含费用)。
纯函数 + 文件存储, 不读行情、不做 API。
"""
from __future__ import annotations

from datetime import UTC, datetime
from datetime import date as _date
from pathlib import Path

from app.definitions import JsonDefinitionStore

# 归因: 只有用户知道当时为什么卖, 固定选项、不由程序或模型判断。顺序即展示顺序。
REASONS: dict[str, str] = {
    "planned_profit": "按计划止盈",
    "planned_stop": "按计划止损",
    "early_exit": "提前卖出",
    "late_stop": "拖延未止损",
    "unplanned": "计划外交易",
}
NOTE_MAX_LEN = 200

_STORE = JsonDefinitionStore("user_data/trade_journal", glob_pattern="tj_*.json", label="trade journal")


def entry_id(lot_id: str) -> str:
    """记录 id 由批次 id 派生: 同一批次重复结清只覆盖同一条记录。"""
    return f"tj_{lot_id}"


def validate_close(lot: dict, sell_price, sell_date, reason, note, *, today: _date) -> None:
    """校验结清输入, 非法抛 ValueError (中文信息)。"""
    if isinstance(sell_price, bool) or not isinstance(sell_price, (int, float)) or not sell_price > 0:
        raise ValueError("卖出价必须是正数")
    try:
        sold = _date.fromisoformat(sell_date)
    except (TypeError, ValueError):
        raise ValueError(f"卖出日必须是 YYYY-MM-DD: {sell_date!r}") from None
    if sold > today:
        raise ValueError("卖出日不能晚于今天")
    if lot.get("buy_date"):
        try:
            bought = _date.fromisoformat(lot["buy_date"])
        except (TypeError, ValueError):
            bought = None
        if bought and sold < bought:
            raise ValueError("卖出日不能早于买入日")
    if reason not in REASONS:
        raise ValueError("归因必须是固定选项之一")
    if not isinstance(note, str) or len(note) > NOTE_MAX_LEN:
        raise ValueError(f"备注最长 {NOTE_MAX_LEN} 字")


def build_entry(lot: dict, *, sell_price: float, sell_date: str, reason: str, note: str, name: str,
                had_invalidation: bool, buy_phase: str | None, hold_days: int | None) -> dict:
    """批次 + 结清输入 → 冻结的交易记录 (不校验)。"""
    return {
        "id": entry_id(lot["id"]),
        "lot_id": lot["id"],
        "symbol": lot["symbol"],
        "name": name or lot["symbol"],
        "qty": lot.get("qty", 0),
        "cost_price": lot["cost_price"],
        "buy_date": lot.get("buy_date") or None,
        "target_pct": lot.get("target_pct", 0),
        "stop_pct": lot.get("stop_pct", 0),
        "sell_price": float(sell_price),
        "sell_date": sell_date,
        "reason": reason,
        "note": note.strip(),
        "had_invalidation": bool(had_invalidation),
        "buy_phase": buy_phase,
        "hold_days": hold_days,
        "created_at": datetime.now(UTC).isoformat(),
    }


def pnl_pct(entry: dict) -> float:
    """盈亏百分数 (2.5 表示 2.5%): 卖出价相对登记成本, 不含费用。"""
    return (float(entry["sell_price"]) / float(entry["cost_price"]) - 1) * 100


def load_all(data_dir: Path) -> list[dict]:
    """读取全部记录; 损坏的文件被跳过。"""
    return _STORE.load_all(data_dir)


def save_one(data_dir: Path, entry: dict) -> None:
    _STORE.save_one(data_dir, entry)


def delete_one(data_dir: Path, entry_id_: str) -> bool:
    return _STORE.delete_one(data_dir, entry_id_)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _group(key: str, label: str, pnls: list[float]) -> dict:
    return {"key": key, "label": label, "total": len(pnls),
            "win_rate": sum(p > 0 for p in pnls) / len(pnls) * 100,
            "avg_pnl_pct": _mean(pnls)}


def stats(entries: list[dict]) -> dict | None:
    """累计统计; 比率均为百分数, 无样本的项为 None。盈亏为 0 的只计入总笔数。"""
    if not entries:
        return None
    from app.services.market_phase import PHASE_LABELS

    pnls = [pnl_pct(e) for e in entries]
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]
    avg_win, avg_loss = _mean(wins), _mean(losses)
    holds = [e["hold_days"] for e in entries if e.get("hold_days") is not None]

    by_reason: dict[str, list[float]] = {}
    by_phase: dict[str, list[float]] = {}
    for e, p in zip(entries, pnls, strict=True):
        by_reason.setdefault(e.get("reason") or "", []).append(p)
        phase = e.get("buy_phase")
        by_phase.setdefault(phase if phase in PHASE_LABELS else "unknown", []).append(p)
    phase_labels = {**PHASE_LABELS, "unknown": "未知"}
    return {
        "total": len(entries),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(entries) * 100,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "payoff_ratio": avg_win / avg_loss if avg_win is not None and avg_loss else None,
        "avg_hold_days": _mean(holds),
        "by_reason": [_group(k, label, by_reason[k]) for k, label in REASONS.items() if k in by_reason],
        "by_phase": [_group(k, label, by_phase[k]) for k, label in phase_labels.items() if k in by_phase],
    }
