"""Freeze prior-session technical evidence during first-board pool preparation."""
from __future__ import annotations

import logging
from datetime import date, timedelta
from math import isfinite

import polars as pl

from app.indicators.pipeline import compute_indicators
from app.services import data_integrity
from app.services.huichun_calendar import BUNDLED_CALENDAR_PATH, load_market_calendar

logger = logging.getLogger(__name__)
VERSION = "ma-macd-shadow-v1"
MA_SESSIONS = 21  # Prior MA20 needs one extra close.
MACD_SESSIONS = 120  # Use exactly this EMA seed window, independent of cache history length.
_VALUES = ("close", "ma5", "ma10", "ma20", "prev_close", "prev_ma5", "prev_ma20",
           "macd_dif", "macd_dea", "macd_hist", "prev_macd_hist")
_INDICATORS = {"ma5", "ma10", "ma20", "macd_dif", "macd_dea", "macd_hist"}
_SHADOW_TYPE = pl.Struct({
    **dict.fromkeys(("version", "reference_date", "pattern", "status", "ma_reason", "macd_reason"), pl.String),
    **dict.fromkeys(("ma_support", "macd_support"), pl.Boolean),
    **dict.fromkeys(_VALUES, pl.Float64),
})


def _sessions(as_of: date) -> list[date]:
    start = as_of - timedelta(days=240)
    with data_integrity._CAL_LOCK:
        calendar = data_integrity._CAL[1]
        if calendar and min(calendar) <= start and as_of <= max(calendar):
            if as_of not in calendar:
                return []
            return sorted(d for d in calendar if d < as_of)[-MACD_SESSIONS:]
    days = load_market_calendar(BUNDLED_CALENDAR_PATH, start=start, end=as_of)
    return days[:-1][-MACD_SESSIONS:] if days and days[-1] == as_of else []


def _values(history: pl.DataFrame, sessions: list[date]) -> dict[str, dict]:
    """Validate each period independently. An older gap only invalidates MACD."""
    if (not sessions or history.is_empty()
            or not {"symbol", "date", "close", "raw_close"}.issubset(history.columns)
            or any(not history.schema[c].is_numeric() for c in ("close", "raw_close"))):
        return {}
    ref = sessions[-1]
    # Anchor prices to the raw reference close. A uniform rebase applied after
    # the reference session cancels from the entire historical series.
    frame = history.filter(pl.col("date").is_in(sessions))
    valid = pl.all_horizontal(pl.col(c).is_finite() & (pl.col(c) > 0) for c in ("close", "raw_close"))
    if "quote_ts" in frame.columns:
        if frame.schema["quote_ts"] != pl.Null and not frame.schema["quote_ts"].is_numeric():
            return {}
        midnight = pl.col("date").cast(pl.Datetime).dt.replace_time_zone("Asia/Shanghai").dt.timestamp("ms")
        # Undated historical daily bars keep their repository semantics.
        # Explicit intraday timestamps, even from T-1, are not completed bars.
        ts = pl.col("quote_ts")
        valid &= ts.is_null() | ((ts >= midnight + 15 * 3_600_000) & (ts < midnight + 24 * 3_600_000))
    frame = frame.filter((pl.len().over("symbol", "date") == 1) & valid.fill_null(False))
    reference = frame.filter(pl.col("date") == ref).select(
        "symbol", (pl.col("raw_close") / pl.col("close")).alias("_scale"),
    )
    frame = frame.join(reference, on="symbol", how="inner").select(
        "symbol", "date", pl.when(pl.col("date") == ref).then(pl.col("raw_close"))
        .otherwise(pl.col("close") * pl.col("_scale")).alias("close"),
    ).filter(pl.col("close").is_finite() & (pl.col("close") > 0)).sort("symbol", "date")
    if frame.is_empty():
        return {}
    # Frames are limited to the independent calendar, duplicate and bad rows
    # have been removed. Equal counts therefore prove full session coverage.
    frame = frame.with_columns(
        (pl.col("date") >= sessions[-min(MA_SESSIONS, len(sessions))]).sum().over("symbol").alias("_ma_bars"),
        pl.len().over("symbol").alias("_macd_bars"),
    )
    frame = compute_indicators(frame, needed=_INDICATORS, assume_sorted=True).with_columns(
        *[pl.col(c).shift(1).over("symbol").alias(f"prev_{c}") for c in ("close", "ma5", "ma20", "macd_hist")],
    ).filter(pl.col("date") == ref)
    return {r["symbol"]: r for r in frame.iter_rows(named=True)}


def _evidence(pattern: str, ref: date | None, values: dict, reason: str) -> dict:
    ma = values.get("_ma_bars") == MA_SESSIONS and all(
        values.get(c) is not None and isfinite(values[c]) for c in _VALUES if "macd" not in c)
    macd = values.get("_macd_bars") == MACD_SESSIONS and all(
        values.get(c) is not None and isfinite(values[c]) for c in _VALUES if "macd" in c)
    result = {"version": VERSION, "reference_date": ref.isoformat() if ref else None, "pattern": pattern,
              "status": "ready" if ma and macd else "partial" if ma or macd else "unavailable",
              "ma_support": None, "macd_support": None,
              "ma_reason": reason or "缺少连续21个有效日收盘，均线待确认",
              "macd_reason": reason or "缺少连续120个有效日收盘，MACD待确认"}
    for key in _VALUES:
        ok = macd if "macd" in key else ma
        result[key] = values.get(key) if ok else None
    if ma:
        close, ma5, ma10, ma20 = (values[c] for c in ("close", "ma5", "ma10", "ma20"))
        if pattern == "platform":
            support, condition = close >= ma20 and ma20 >= values["prev_ma20"], "收盘不低于MA20且MA20未下行"
        elif pattern == "trend":
            support, condition = close >= ma5 > ma10 > ma20, "收盘不低于MA5且MA5>MA10>MA20"
        else:
            support = values["prev_close"] < values["prev_ma5"] and close >= ma5
            condition = "收盘从MA5下方重新站上MA5"
        result.update(ma_support=support, ma_reason=("符合：" if support else "未满足：") + condition)
    if macd:
        dif, dea, hist = (values[c] for c in ("macd_dif", "macd_dea", "macd_hist"))
        if pattern == "platform":
            support, condition = dif > dea, "DIF高于DEA"
        elif pattern == "trend":
            support, condition = dif > 0 and hist > 0, "DIF为正且柱为正"
        else:
            support, condition = values["prev_macd_hist"] < hist < 0, "负柱较前一交易日收缩"
        result.update(macd_support=support, macd_reason=("符合：" if support else "未满足：") + condition)
    return result


class FirstBoardTechnical:
    """Single-worker cache of validated indicators, independent of intraday updates."""

    def __init__(self, repo):
        self._repo = repo
        self._signature = None
        self._values: dict[str, dict] = {}

    def attach(self, candidates: pl.DataFrame, *, as_of: date, reference: date | None) -> pl.DataFrame:
        if candidates.is_empty():
            return candidates
        reason = "候选参考日与已完成交易日尚未核对"
        values = {}
        confirmed_reference = None
        if reference is not None:
            try:
                sessions = _sessions(as_of)
                if sessions and sessions[-1] == reference:
                    confirmed_reference = reference
                    history, signature = self._repo.get_first_board_technical_closes(
                        candidates["symbol"].unique().to_list(), sessions, self._signature)
                    if history is not None:
                        self._values = _values(history, sessions)
                        self._signature = signature
                    values = self._values
                    reason = ""
            except Exception:  # noqa: BLE001 - optional evidence must never block live candidates
                logger.warning("首板前一交易日技术证据暂不可用", exc_info=True)
                reason = "交易日历或历史日线读取失败，技术证据待确认"
        rows = [_evidence(pattern, confirmed_reference, values.get(symbol, {}), reason)
                if confirmed_reference is None or seed_day == confirmed_reference
                else _evidence(pattern, None, {}, "候选参考日与交易日历不一致")
                for symbol, pattern, seed_day in candidates.select("symbol", "pattern", "reference_date").iter_rows()]
        return candidates.with_columns(pl.Series("technical_shadow", rows, dtype=_SHADOW_TYPE))
