"""Read-only overseas index display via the overseas fallback chain (one source per request)."""
from __future__ import annotations

import logging
import math
from collections import Counter
from datetime import date

from app.data_providers import routing_health
from app.services import preferences
from app.services.index_const import OVERSEAS_INDEX_SYMBOLS
from app.services.shared_work import SharedWork

logger = logging.getLogger(__name__)
_work = SharedWork("http.kline", max_pending=12, max_entries=32)


def _read(method: str, *args) -> dict:
    """沿外围行情兜底链请求一次; 结果只来自一个源 (source 为实际来源)。

    空 list 是有效结果 (休市日), 不触发换源; 异常和非 list 才算失败。
    """
    ttl = 60 if method == "get_overseas_index_quotes" else 300

    def invoke(source, call):
        # 缓存键含源名与绑定方法: 换源不命中旧源缓存, 插件重载后的新实例也不命中旧缓存
        return _work.run((source, getattr(routing_health.resolve_provider(source), method), *args),
                         call, ttl_s=ttl)

    primary = preferences.get_overseas_data_provider()
    result = routing_health.call_with_fallback(
        "overseas", method, *args, validate=lambda rows: isinstance(rows, list), invoke=invoke)
    # 全链失败时没有任何源提供数据, 不标「兜底」(只给 reason)
    out = {"source": result.source or primary, "rows": result.data if result.source else [],
           "degraded": bool(result.source) and result.degraded, "fallback_reason": result.reason}
    if result.source is None:
        outcomes = {outcome for _, outcome in result.attempts}
        if not result.attempts:
            out["reason"] = "所选数据源未提供海外行情"
        elif outcomes == {"unresolved"}:
            out["reason"] = "所选海外数据源未提供此项指数数据"
        else:
            logger.warning("海外指数请求失败 (%s): %s", method, result.attempts)
            out["reason"] = "海外数据源暂不可用，请稍后重试"
    return out


def _finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def get_quotes() -> dict:
    result = _read("get_overseas_index_quotes")
    rows = []
    candidates = [row for row in result["rows"] if isinstance(row, dict)
                  and isinstance(row.get("symbol"), str) and row["symbol"] in OVERSEAS_INDEX_SYMBOLS]
    counts = Counter(row["symbol"] for row in candidates)
    for row in candidates:
        if counts[row["symbol"]] != 1 or row.get("state") != "ready":
            continue
        try:
            day = date.fromisoformat(row["session_date"])
            price, prev, ratio = row["price"], row["previous_close"], row["change_pct"]
            if (not all(_finite(v) for v in (price, prev, ratio))
                    or min(price, prev) <= 0 or not math.isfinite(ratio * 100)
                    or not math.isclose(ratio, price / prev - 1, rel_tol=1e-6, abs_tol=1e-8)):
                continue
        except (TypeError, ValueError, KeyError, OverflowError):
            continue
        rows.append({"symbol": row["symbol"], "date": day.isoformat(), "last_price": price,
                     "change_pct": ratio * 100})
    return {**result, "rows": rows}


def get_daily(symbol: str, start: date, end: date) -> dict:
    result = _read("get_overseas_index_daily", symbol, start, end)
    rows = []
    try:
        for row in result["rows"]:
            day = date.fromisoformat(row["date"])
            if row["symbol"] != symbol or not start <= day <= end:
                continue
            date_str = day.isoformat()
            if rows and date_str <= rows[-1]["date"]:
                raise ValueError("重复或错序的日线")
            ohlc = {key: row[key] for key in ("open", "high", "low", "close")}
            if not all(_finite(v) and v > 0 for v in ohlc.values()):
                continue
            if not ohlc["low"] <= min(ohlc["open"], ohlc["close"]) <= max(ohlc["open"], ohlc["close"]) <= ohlc["high"]:
                continue
            volume = row.get("volume")
            rows.append({"symbol": symbol, "date": date_str, **ohlc,
                         "volume": volume if _finite(volume) and volume >= 0 else None})
    except (KeyError, ValueError, TypeError, OverflowError):
        return {**result, "rows": [], "reason": "海外指数日线格式无法核验"}
    return {**result, "rows": rows}
