"""Post-prediction observations, never fed back into the frozen forecast.

Daily bars cannot establish the ordering of intraday trigger/exit conditions.
These figures therefore describe the watchlist, not filled trades or strategy P&L.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from app.market_time import CN_TZ, cn_now
from app.services.index_const import CORE_INDEX_SYMBOLS
from app.services.market_game_plan import plan_status


def _day(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _positive(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def _closing_evidence(repo, table: str, symbols: list[str], start: date, end: date,
                      known_by: datetime) -> dict[tuple[str, date], tuple[float, datetime, float | None, float | None]]:
    """Verify raw closing observations through the repository's read API.

    A null quote timestamp is commonly a historical batch row, but it is also
    possible for a realtime supplier to omit its timestamp. Without separate
    batch provenance this observer cannot certify it merely from that null.
    """
    if not symbols or start > end:
        return {}
    try:
        columns = {row[0] for row in repo.execute_all(f"DESCRIBE {table}")}
        if not {"symbol", "date", "close", "quote_ts"} <= columns:
            return {}
        # Table identifiers are constants supplied by this module. Symbols and
        # dates remain bound parameters, including imported report identifiers.
        marks = ",".join("?" for _ in symbols)
        high_column = "high" if "high" in columns else "NULL"
        low_column = "low" if "low" in columns else "NULL"
        rows = repo.execute_all(
            f"SELECT symbol,date,close,quote_ts,{high_column},{low_column} FROM {table} "
            f"WHERE date >= ? AND date <= ? AND symbol IN ({marks})",
            [start, end, *symbols],
        )
    except Exception:
        return {}
    result, seen, duplicates = {}, set(), set()
    for symbol, raw_day, raw_close, quote_ts, raw_high, raw_low in rows:
        try:
            day = _day(raw_day)
            key = (symbol, day)
            if key in seen:
                duplicates.add(key)
            seen.add(key)
            price, stamp = _positive(raw_close), _positive(quote_ts)
            if price is None or stamp is None or not start <= day <= end:
                continue
            observed = datetime.fromtimestamp(stamp / 1000, tz=CN_TZ)
            if observed.date() != day or observed.hour < 15 or observed > known_by:
                continue
            result[key] = (price, observed, _positive(raw_high), _positive(raw_low))
        except (TypeError, ValueError, OSError, OverflowError):
            continue
    for key in duplicates:
        result.pop(key, None)
    return result


def _matches(evidence: dict, key: tuple[str, date], raw_close: float | None,
             known_by: datetime) -> bool:
    value = evidence.get(key)
    return bool(value and raw_close and value[1] <= known_by
                and math.isclose(value[0], raw_close, rel_tol=1e-8, abs_tol=1e-6))


def _range_matches(evidence: dict, key: tuple[str, date], high: float | None,
                   low: float | None, close: float | None) -> bool:
    raw = evidence.get(key)
    if not raw or not high or not low or not close or not raw[2] or not raw[3]:
        return False
    scale = raw[0] / close
    return (math.isclose(high * scale, raw[2], rel_tol=1e-8, abs_tol=1e-6)
            and math.isclose(low * scale, raw[3], rel_tol=1e-8, abs_tol=1e-6))


def evaluate_observations(repo, report: dict, *, now: datetime | None = None) -> dict:
    now = (now or cn_now()).astimezone(CN_TZ)
    base_day = date.fromisoformat(report["as_of"])
    cutoff = datetime.fromisoformat(report.get("cutoff") or f"{base_day}T15:00:00+08:00").astimezone(CN_TZ)
    # Latest mode may use old daily prices on a holiday. Outcomes must still
    # occur after the actual observation cutoff, never before issuance.
    after_day = max(base_day, cutoff.date() if (cutoff.hour, cutoff.minute) >= (9, 30) else cutoff.date() - timedelta(days=1))
    completed = now.date() if (now.hour, now.minute) >= (15, 30) else now.date() - timedelta(days=1)
    # A bounded read is sufficient for three sessions, including long closures.
    end = min(completed, base_day + timedelta(days=45))
    limitations = [
        "仅观察名单的收盘表现, 不代表满足入场条件、已经成交或扣费策略收益。",
        "日序来自核心指数已观测交易日期, 独立交易日历完整性未验证; 缺失市场日期可能影响日序。",
        "收益使用同次读取的前复权收盘序列; 不以报告原始报价跨除权直接相除。",
        "买入后受 T+1、停牌、涨跌停及滑点约束; 本观察表不模拟成交或退出。",
        "基准、后续股价和指数均需原始收盘时间证据; 时间缺失或原始价与复权记录不匹配时标为无法核验, 不推定已完成收盘。",
    ]
    result = {
        "report_id": report["id"], "as_of": report["as_of"],
        "evaluated_at": now.isoformat(timespec="seconds"),
        "status": "pending", "kind": "observation_only", "rows": [],
        "limitations": limitations,
        "plan_status": plan_status(report, now),
    }
    validity = report.get("validity") or {}
    calendar_verified = validity.get("calendar_verified") is True and validity.get("status") == "scheduled"
    frozen_sessions = [_day(day) for day in validity.get("observation_sessions", [])] if calendar_verified else []
    result["calendar_verified"] = calendar_verified
    if calendar_verified:
        limitations[1] = "观察日使用生成时冻结的独立交易日历, 行情缺日不会顺延日序。"
    candidates = report.get("candidates") or []
    if not candidates:
        result["status"] = "unavailable"
        limitations.append("该报告没有候选, 不计算收益或胜率。")
        return result
    sessions: list[date] = []
    prices: dict[tuple[str, date], float | None] = {}
    raw_prices: dict[tuple[str, date], float | None] = {}
    index_prices: dict[date, float | None] = {}
    ranges: dict[tuple[str, date], tuple[float | None, float | None]] = {}
    stock_evidence, index_evidence = {}, {}
    if end > base_day:
        frame = repo.get_index_daily(CORE_INDEX_SYMBOLS[0], base_day, end, columns=["date", "close"])
        if not frame.is_empty() and {"date", "close"}.issubset(frame.columns):
            sessions = frozen_sessions or sorted({
                _day(row["date"]) for row in frame.iter_rows(named=True)
                if after_day < _day(row["date"]) <= end and _positive(row["close"]) is not None
            })[:3]
            for row in frame.iter_rows(named=True):
                day = _day(row["date"])
                if day in sessions or day == base_day:
                    # Multiple index rows cannot certify one closing value.
                    index_prices[day] = None if day in index_prices else _positive(row["close"])
        if frozen_sessions:
            sessions = frozen_sessions
        if sessions:
            symbols = [item["symbol"] for item in candidates]
            read_end = min(end, sessions[-1])
            stock_evidence = _closing_evidence(repo, "kline_daily", symbols, base_day, read_end, now)
            index_evidence = _closing_evidence(repo, "kline_index_daily", [CORE_INDEX_SYMBOLS[0]], base_day, read_end, now)
            frame = repo.get_daily_batch(
                symbols, base_day, read_end,
                columns=["symbol", "date", "close", "high", "low", "raw_close"],
            )
            if not frame.is_empty() and {"symbol", "date", "close"}.issubset(frame.columns):
                duplicates: set[tuple[str, date]] = set()
                for row in frame.iter_rows(named=True):
                    day = _day(row["date"])
                    if not base_day <= day <= read_end:
                        continue
                    key = (row["symbol"], day)
                    if key in prices:
                        duplicates.add(key)
                    prices[key] = _positive(row["close"])
                    raw_prices[key] = _positive(row.get("raw_close"))
                    high, low = _positive(row.get("high")), _positive(row.get("low"))
                    close = prices[key]
                    ranges[key] = (high, low) if high and low and close and low <= close <= high else (None, None)
                for key in duplicates:
                    prices[key] = None
                    raw_prices[key] = None
                    ranges[key] = (None, None)
    if frozen_sessions:
        sessions = frozen_sessions
    for candidate in candidates:
        symbol = candidate["symbol"]
        base = prices.get((symbol, base_day))
        for horizon in range(1, 4):
            day = sessions[horizon - 1] if len(sessions) >= horizon else None
            close = prices.get((symbol, day)) if day else None
            if day is None or day > completed:
                state = "pending"
            elif not base or not close:
                state = "missing"
            elif not (
                _matches(stock_evidence, (symbol, base_day), raw_prices.get((symbol, base_day)), cutoff)
                and _matches(stock_evidence, (symbol, day), raw_prices.get((symbol, day)), now)
                and _matches(index_evidence, (CORE_INDEX_SYMBOLS[0], day), index_prices.get(day), now)
            ):
                state = "unavailable"
            else:
                state = "available"
            close_return = round(close / base - 1, 6) if state == "available" else None
            benchmark = index_prices.get(day)
            benchmark_base = index_prices.get(base_day)
            benchmark_return = None
            if (state == "available" and benchmark and benchmark_base
                    and _matches(index_evidence, (CORE_INDEX_SYMBOLS[0], base_day), benchmark_base, cutoff)):
                benchmark_return = round(benchmark / benchmark_base - 1, 6)
            # Bounds describe the watchlist's price excursion from report close,
            # not trade MFE/MAE: neither an entry time nor a fill is established.
            observed_ranges = [ranges.get((symbol, observed), (None, None)) for observed in sessions[:horizon]]
            ranges_verified = state == "available" and all(
                _matches(stock_evidence, (symbol, observed), raw_prices.get((symbol, observed)), now)
                and _range_matches(stock_evidence, (symbol, observed), high, low, prices.get((symbol, observed)))
                for observed, (high, low) in zip(sessions[:horizon], observed_ranges, strict=True)
            )
            result["rows"].append({
                "symbol": symbol, "name": candidate["name"], "horizon": horizon,
                "label": f"后续观察日 {horizon}", "trade_date": day.isoformat() if day else None,
                "close_return": close_return,
                "benchmark_return": benchmark_return,
                "excess_return": round(close_return - benchmark_return, 6) if benchmark_return is not None else None,
                "max_upside": round(max(high for high, _ in observed_ranges) / base - 1, 6) if ranges_verified else None,
                "max_drawdown": round(min(low for _, low in observed_ranges) / base - 1, 6) if ranges_verified else None,
                "state": state,
            })
    states = [row["state"] for row in result["rows"]]
    if all(state == "available" for state in states):
        result["status"] = "complete"
    elif any(state != "pending" for state in states):
        result["status"] = "partial"
    return result
