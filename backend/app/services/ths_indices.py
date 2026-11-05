"""同花顺专有指数的独立、只读展示链路。"""
from __future__ import annotations

import logging
import math
from calendar import monthrange
from datetime import date, datetime, timedelta

import polars as pl

from app.data_providers import custom
from app.indicators.pipeline import compute_enriched
from app.market_time import CN_TZ, cn_now
from app.services import preferences
from app.services.index_const import THS_ALL_A_SYMBOL
from app.services.shared_work import SharedWork

logger = logging.getLogger(__name__)
_work = SharedWork("http.kline", max_pending=8, max_entries=32)


def _provider():
    source = preferences.get_ths_index_data_provider()
    if not custom.provider_has_dataset(source, "ths_index"):
        raise ValueError("所选数据源未提供同花顺指数")
    return source, custom.get_provider(source)


def get_quotes() -> dict:
    source = preferences.get_ths_index_data_provider()
    try:
        source, provider = _provider()
        fetch = provider.get_ths_index_quotes
        records = _work.run((source, fetch), fetch, ttl_s=5)
        rows = []
        now = cn_now()
        for rec in records:
            if rec.get("symbol") != THS_ALL_A_SYMBOL:
                continue
            price, previous, timestamp = (rec.get(key) for key in ("last_price", "prev_close", "snapshot_time"))
            if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0
                   for v in (price, previous, timestamp)):
                continue
            observed = datetime.fromtimestamp(timestamp / 1000, CN_TZ)
            if observed > now + timedelta(seconds=5):
                continue
            change_pct = (price / previous - 1) * 100
            if not math.isfinite(change_pct):
                continue
            rows.append({
                "symbol": THS_ALL_A_SYMBOL, "last_price": price, "prev_close": previous,
                "change_pct": change_pct, "snapshot_time": timestamp,
                "date": None, "source": source,
            })
        if len(rows) != 1:
            return {"source": source, "rows": [], "reason": "未取得可核验的全A报价，请检查同花顺指数数据源"}
        return {"source": source, "rows": rows}
    except Exception:  # noqa: BLE001 - optional source failure stays within this page
        logger.warning("同花顺指数报价暂不可用 (%s)", source)
        return {"source": source, "rows": [], "reason": "全A报价更新失败，请检查同花顺指数数据源"}


def get_daily(symbol: str, start: date, end: date) -> dict:
    source = preferences.get_ths_index_data_provider()
    if symbol != THS_ALL_A_SYMBOL:
        return {"source": source, "rows": [], "reason": "不支持此同花顺指数"}
    today = cn_now().date()
    earliest = date(today.year - 5, today.month, min(today.day, monthrange(today.year - 5, today.month)[1]))
    if start < earliest:
        return {"source": source, "rows": [], "reason": f"全A日K仅支持近五年，请选择 {earliest} 及之后的日期"}
    try:
        source, provider = _provider()
        fetch = provider.get_ths_index_daily

        def acquire():
            # Warm moving averages on earlier actual bars before trimming the requested window.
            frame = fetch(symbol, max(earliest, start - timedelta(days=300)), end)
            if not isinstance(frame, pl.DataFrame):
                raise ValueError("同花顺指数日K格式无效")
            if frame.is_empty():
                return []
            frame = frame.filter(pl.col("symbol") == symbol).sort("date")
            if frame["date"].n_unique() != frame.height:
                raise ValueError("同花顺指数日K日期重复")
            return compute_enriched(frame, factors=None, instruments=None).filter(
                pl.col("date").is_between(start, end)
            ).to_dicts()

        rows = _work.run((source, fetch, symbol, start, end), acquire, ttl_s=60)
        return {"source": source, "rows": rows,
                **({"reason": "所选日期暂无全A日K数据"} if not rows else {})}
    except Exception:  # noqa: BLE001
        logger.warning("同花顺指数日K暂不可用 (%s)", source)
        return {"source": source, "rows": [], "unavailable": True,
                "reason": "全A日K暂不可用，请检查同花顺指数数据源"}
