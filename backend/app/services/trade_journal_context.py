"""结清批次时冻结进交易记录的上下文: 持有交易日数、买入日情绪周期阶段。

任何一项取不到都返回 None (宁缺勿错): 不用自然日替代交易日, 不用相邻日阶段顶替。
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)


def _day(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def hold_days(buy_date: str | None, sell_date: str) -> int | None:
    """买入日到卖出日的交易日数 (建仓当日记零, 与持仓分析同口径)。

    任一日期缺失、不是交易日或日历不覆盖时为 None。
    """
    bought, sold = _day(buy_date), _day(sell_date)
    if bought is None or sold is None or sold < bought:
        return None
    try:
        from app.services.huichun_calendar import BUNDLED_CALENDAR_PATH, load_market_calendar

        sessions = load_market_calendar(BUNDLED_CALENDAR_PATH, start=bought, end=sold)
    except Exception as e:  # noqa: BLE001 - 日历缺失/不覆盖只降级为未知
        logger.debug("trade journal: calendar unavailable for %s..%s: %s", buy_date, sell_date, e)
        return None
    if not sessions or sessions[0] != bought or sessions[-1] != sold:
        return None
    return len(sessions) - 1


def buy_phase(data_dir: Path, buy_date: str | None) -> str | None:
    """买入日的情绪周期阶段 key (regime 历史); 无该日或未标注为 None。"""
    bought = _day(buy_date)
    if bought is None:
        return None
    try:
        import polars as pl

        from app.services import regime_builder

        df = regime_builder.load_regime_history(data_dir)
        if df.is_empty() or "date" not in df.columns or "phase" not in df.columns:
            return None
        rows = df.filter(pl.col("date") == bought)["phase"].to_list()
    except Exception as e:  # noqa: BLE001
        logger.debug("trade journal: regime phase unavailable for %s: %s", buy_date, e)
        return None
    return rows[-1] or None if rows else None
