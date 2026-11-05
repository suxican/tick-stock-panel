"""Read-only overseas index display using the selected overseas provider."""
from __future__ import annotations

import logging
import math
from collections import Counter
from datetime import date

from app.data_providers import custom
from app.services import preferences
from app.services.index_const import OVERSEAS_INDEX_SYMBOLS
from app.services.shared_work import SharedWork

logger = logging.getLogger(__name__)
_work = SharedWork("http.kline", max_pending=12, max_entries=32)


def _read(method: str, *args) -> dict:
    source = preferences.get_overseas_data_provider()
    try:
        if not custom.provider_has_dataset(source, "overseas"):
            return {"source": source, "rows": [], "reason": "所选数据源未提供海外行情"}
        provider = custom.get_provider(source)
        fetch = getattr(provider, method, None)
        if not callable(fetch):
            return {"source": source, "rows": [], "reason": "所选海外数据源未提供此项指数数据"}
        # The bound method separates reloaded provider instances even under the same name.
        ttl = 60 if method == "get_overseas_index_quotes" else 300
        rows = _work.run((source, fetch, *args), lambda: fetch(*args), ttl_s=ttl)
        if not isinstance(rows, list):
            raise ValueError("海外指数数据格式无效")
        return {"source": source, "rows": rows}
    except Exception:  # noqa: BLE001 - isolate optional providers and limit bursts on failure
        logger.warning("海外指数请求失败 (%s/%s)", source, method)
        return {"source": source, "rows": [], "reason": "海外数据源暂不可用，请稍后重试"}


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
