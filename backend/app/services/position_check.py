"""复盘仓位核对: 复盘日收盘持仓占比 vs 前一交易日收盘后定下的环境结论上限。

回答"我有没有按自己的规则做"。上限来自环境结论存档(environment_conclusion_archive),
取前一交易日存档中、复盘日 09:30 之前的最后一版——盘前改预设算数, 开盘后改的不算当天。

单位: 账户规模与市值为人民币元; ratio_pct / cap_pct 为百分数。
任一输入缺失时 exceeded 为 None 并写明原因, 不给出"未超限"。恰好等于上限不算超限。
账户规模与市值只在页面展示, 不进提示词(见 market_recap_execution)。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time
from pathlib import Path

from app.market_time import CN_TZ
from app.services import preferences
from app.services.environment_conclusion_archive import load_day
from app.services.regime_builder import load_regime_history

logger = logging.getLogger(__name__)

STATS_WINDOW = 120
_OPEN = time(9, 30)
_TIER = {"strong": "强", "range": "震荡", "weak": "弱"}
_VOLUME = {"expand": "放量", "flat": "平量", "shrink": "缩量"}


def cell_label(cell: str | None) -> str | None:
    if not cell or "_" not in cell:
        return None
    tier, volume = cell.split("_", 1)
    return f"{_TIER.get(tier, tier)}·{_VOLUME.get(volume, volume)}"


def _beijing_day(stamp: str | None) -> str | None:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(CN_TZ)
    return moment.date().isoformat()


def holdings_on(open_lots: list[dict], entries: list[dict], day: str) -> tuple[dict[str, float], int, int]:
    """day 收盘时的持仓 {symbol: 数量}, 以及买入日按登记日推断的未结清批次数、买入日未知的已结清记录数。"""
    holdings: dict[str, float] = {}
    inferred = unknown = 0

    def add(symbol: str, qty) -> None:
        holdings[symbol] = holdings.get(symbol, 0.0) + float(qty or 0)

    for lot in open_lots:
        bought = lot.get("buy_date") or None
        if bought is None:
            inferred += 1
            bought = _beijing_day(lot.get("created_at"))
        if bought is not None and bought <= day:
            add(lot["symbol"], lot.get("qty"))
    for entry in entries:
        bought = entry.get("buy_date") or None
        if bought is None:
            unknown += 1
            continue
        sold = entry.get("sell_date") or ""
        if bought <= day < sold:
            add(entry["symbol"], entry.get("qty"))
    return holdings, inferred, unknown


def _trading_days(data_dir: Path) -> list[str]:
    df = load_regime_history(data_dir)
    if df.is_empty() or "date" not in df.columns:
        return []
    return sorted(str(d) for d in df["date"].to_list())


def _previous(days: list[str], day: str) -> str | None:
    earlier = [d for d in days if d < day]
    return earlier[-1] if earlier else None


def _cap_from_archive(data_dir: Path, previous: str | None, day: str) -> dict:
    out = {"cap_pct": None, "cap_date": previous, "cell": None, "reason": None}
    if previous is None:
        out["reason"] = "缺少前一交易日"
        return out
    archive = load_day(data_dir, previous)
    cutoff = datetime.combine(date.fromisoformat(day), _OPEN, CN_TZ)
    chosen = None
    for version in (archive or {}).get("versions", []):
        try:
            recorded = datetime.fromisoformat(version["recorded_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=CN_TZ)
        if recorded < cutoff:
            chosen = version
    if chosen is None:
        out["reason"] = "前一交易日无结论存档"
        return out
    conclusion = chosen.get("conclusion") or {}
    out["cell"] = conclusion.get("cell")
    cap = conclusion.get("effective_cap_pct")
    if cap is None:
        out["reason"] = "前一交易日该格未设置预设"
    else:
        out["cap_pct"] = cap
    return out


def applicable_cap(data_dir: Path, day: str) -> dict:
    return _cap_from_archive(data_dir, _previous(_trading_days(data_dir), day), day)


def check_day(day: str, holdings: dict[str, float], closes: dict[str, float], account_size: float | None,
              cap: dict, *, intraday: bool = False, inferred: int = 0, unknown: int = 0) -> dict:
    out = {"date": day, "position_value": None, "account_size": account_size, "ratio_pct": None,
           "cap_pct": cap.get("cap_pct"), "cap_date": cap.get("cap_date"), "cell": cap.get("cell"),
           "cell_label": cell_label(cap.get("cell")), "exceeded": None,
           "inferred_lots": inferred, "unknown_lots": unknown, "reason": None}
    if intraday:
        out["reason"] = "盘中不判定"
        return out
    if account_size is None:
        out["reason"] = "未填写账户规模"
        return out
    held = {symbol: qty for symbol, qty in holdings.items() if qty}
    if any(closes.get(symbol) is None for symbol in held):
        out["reason"] = "缺收盘价"
        return out
    value = sum(qty * closes[symbol] for symbol, qty in held.items())
    out["position_value"] = round(value, 2)
    out["ratio_pct"] = round(value / account_size * 100, 2)
    if cap.get("cap_pct") is None:
        out["reason"] = cap.get("reason") or "无适用上限"
        return out
    out["exceeded"] = value > account_size * cap["cap_pct"] / 100
    return out


def _closes_by_day(repo, start: str, end: str, symbols: list[str]) -> dict[str, dict[str, float]]:
    getter = getattr(repo, "get_enriched_range", None)
    if getter is None or not symbols:
        return {}
    try:
        df = getter(date.fromisoformat(start), date.fromisoformat(end), symbols=symbols,
                    columns=["symbol", "date", "close", "raw_close"])
    except Exception as exc:  # noqa: BLE001 - 缺价只让核对不可判定, 不影响复盘
        logger.debug("position check: closes unavailable: %s", exc)
        return {}
    out: dict[str, dict[str, float]] = {}
    for row in (df.to_dicts() if df is not None else []):
        value = row.get("raw_close") if row.get("raw_close") is not None else row.get("close")
        if value is not None:
            out.setdefault(str(row["date"]), {})[row["symbol"]] = float(value)
    return out


def build_position_check(repo, as_of: str, open_lots: list[dict], entries: list[dict], *,
                         intraday: bool) -> tuple[dict, dict]:
    """返回 (复盘日核对, 累计统计)。累计只计可判定的日子, 收盘价一次区间读取。"""
    data_dir = Path(repo.store.data_dir)
    history = preferences.get_account_size_history()
    days = _trading_days(data_dir)
    past = [d for d in days if d < as_of]
    candidates = []
    for day in past:
        previous = _previous(days, day)
        if previous is not None and load_day(data_dir, previous):
            candidates.append(day)
    candidates = candidates[-(STATS_WINDOW - 1):]

    plans = {}
    for day in [*candidates, as_of]:
        holdings, inferred, unknown = holdings_on(open_lots, entries, day)
        plans[day] = (holdings, inferred, unknown, _cap_from_archive(data_dir, _previous(days, day), day))
    symbols = sorted({symbol for holdings, *_ in plans.values() for symbol, qty in holdings.items() if qty})
    start = candidates[0] if candidates else as_of
    closes = _closes_by_day(repo, start, as_of, symbols)

    results = {}
    for day, (holdings, inferred, unknown, cap) in plans.items():
        results[day] = check_day(day, holdings, closes.get(day, {}), preferences.account_size_on(history, day), cap,
                                 intraday=intraday and day == as_of, inferred=inferred, unknown=unknown)

    decided = [r for r in results.values() if r["exceeded"] is not None]
    by_cell: dict[str, dict] = {}
    for r in decided:
        row = by_cell.setdefault(r["cell"], {"cell": r["cell"], "label": cell_label(r["cell"]), "checked": 0, "exceeded": 0})
        row["checked"] += 1
        row["exceeded"] += int(r["exceeded"])
    order = {cell: i for i, cell in enumerate(preferences.ENV_MATRIX_CELLS)}
    stats = {"checked": len(decided), "exceeded": sum(int(r["exceeded"]) for r in decided),
             "by_cell": sorted(by_cell.values(), key=lambda row: order.get(row["cell"], len(order)))}
    return results[as_of], stats
