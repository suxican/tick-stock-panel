"""AI 复盘「今日执行」—— 核验的不是对市场的判断, 而是自己的交易。

数据全部来自本地: 批次提醒 (未结清持仓) 与交易记录 (结清留痕)。结论由程序给出,
模型只解释:
- opened:  复盘日买入的批次, 是否已登记研究失效条件;
- closed:  复盘日结清的记录, 盈亏与用户自选的归因;
- stop_breached: 设了止损比例、复盘日收盘已触及止损价却仍未结清的批次
  (盘中或取不到收盘价时不判定, 宁缺勿错);
- stats:   截至复盘日的累计统计。

提示词文本段不含数量、价格、成本金额与备注 (个人持仓细节不发给模型)。
"""
from __future__ import annotations

import logging
from datetime import date

from app.services.market_recap_holdings import _raw_closes
from app.strategy import lots as lots_domain
from app.strategy import trade_journal

logger = logging.getLogger(__name__)


def _names(repo, symbols: list[str]) -> dict[str, str]:
    try:
        return repo.get_name_map(symbols) or {}
    except Exception as e:  # noqa: BLE001 - 名称只用于展示
        logger.debug("recap execution: names unavailable: %s", e)
        return {}


def _stop_breached(repo, snapshot: dict, open_lots: list[dict], names: dict[str, str]) -> list[dict]:
    watched = [lot for lot in open_lots if float(lot.get("stop_pct") or 0) > 0]
    if not watched or snapshot.get("intraday"):
        return []
    closes = _raw_closes(repo, date.fromisoformat(snapshot["as_of"]), sorted({lot["symbol"] for lot in watched}))
    if closes is None:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for lot in watched:
        symbol = lot["symbol"]
        close = closes.get(symbol)
        # 止损价与批次派生监控规则同式 (lots.lot_to_rules)
        stop_price = round(float(lot["cost_price"]) * (1 - float(lot["stop_pct"]) / 100), 4)
        if close is None or symbol in seen or close > stop_price:
            continue
        seen.add(symbol)
        out.append({"symbol": symbol, "name": names.get(symbol) or symbol,
                    "stop_price": stop_price, "close": float(close)})
    return out


def build_execution(repo, snapshot: dict | None) -> dict | None:
    """复盘日的执行情况; 没有任何可展示内容时返回 None。"""
    if not snapshot:
        return None
    from app.services.holding_analysis import lots_invalidation_flags

    as_of = snapshot["as_of"]
    data_dir = repo.store.data_dir
    open_lots = lots_domain.load_all(data_dir)
    entries = trade_journal.load_all(data_dir)
    names = _names(repo, sorted({lot["symbol"] for lot in open_lots}))

    opened_lots = [lot for lot in open_lots if lot.get("buy_date") == as_of]
    flags = lots_invalidation_flags(data_dir, [lot["symbol"] for lot in opened_lots])
    opened: dict[str, dict] = {}
    for lot in opened_lots:
        symbol = lot["symbol"]
        opened.setdefault(symbol, {"symbol": symbol, "name": names.get(symbol) or symbol,
                                   "has_invalidation": flags[symbol]})
    for e in entries:  # 当日买入当日又结清的也算今天的新建
        if e.get("buy_date") == as_of:
            opened.setdefault(e["symbol"], {"symbol": e["symbol"], "name": e.get("name") or e["symbol"],
                                            "has_invalidation": bool(e.get("had_invalidation"))})

    closed = [{
        "symbol": e["symbol"],
        "name": e.get("name") or e["symbol"],
        "pnl_pct": trade_journal.pnl_pct(e),
        "hold_days": e.get("hold_days"),
        "reason": e.get("reason"),
        "reason_label": trade_journal.REASONS.get(e.get("reason"), "未知"),
    } for e in entries if e.get("sell_date") == as_of]

    breached = _stop_breached(repo, snapshot, open_lots, names)
    stats = trade_journal.stats([e for e in entries if (e.get("sell_date") or "") <= as_of])
    if not (opened or closed or breached or stats):
        return None
    return {"as_of": as_of, "opened": list(opened.values()), "closed": closed,
            "stop_breached": breached, "stats": stats}


def _pct(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}%" if signed else f"{v:.0f}%"


def execution_prompt_block(execution: dict | None) -> str:
    """今日执行 → 提示词文本。只含名称、比率、天数与归因中文名; 模型不得改判。"""
    if not execution:
        return ""
    lines = ["## 今日执行(程序判定)"]
    if execution.get("opened"):
        lines.append("- 新建: " + "、".join(
            f"{o['name']}({'已登记失效条件' if o['has_invalidation'] else '未登记失效条件'})"
            for o in execution["opened"]))
    for c in execution.get("closed") or []:
        hold = f", 持有 {c['hold_days']} 个交易日" if c.get("hold_days") is not None else ""
        lines.append(f"- 结清: {c['name']} {_pct(c['pnl_pct'], signed=True)}{hold}, {c['reason_label']}")
    if execution.get("stop_breached"):
        lines.append("- 触及止损价仍持有: " + "、".join(b["name"] for b in execution["stop_breached"]))
    stats = execution.get("stats")
    if stats:
        payoff = f"{stats['payoff_ratio']:.2f}" if stats.get("payoff_ratio") is not None else "—"
        hold = f"{stats['avg_hold_days']:.1f} 个交易日" if stats.get("avg_hold_days") is not None else "—"
        lines.append(f"- 累计 {stats['total']} 笔: 胜率 {_pct(stats.get('win_rate'))}, 盈亏比 {payoff}, 平均持有 {hold}")
        groups = "; ".join(
            f"{g['label']} {g['total']} 笔(胜率 {_pct(g['win_rate'])}, 平均 {_pct(g['avg_pnl_pct'], signed=True)})"
            for g in stats.get("by_reason") or [])
        if groups:
            lines.append(f"- 按归因: {groups}")
        if stats["total"] < 10:
            lines.append("- 样本不足 10 笔, 统计只作参考")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)
