"""Read-only inputs for the explicitly idealized first-board price study.

Raw prices are scale-equivalent to adjusted prices only inside event-free
windows. Corporate-action windows are excluded; no factor or price is repaired.
Current instrument names define the study universe, not historical ST evidence.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from datetime import date

import polars as pl

from app.indicators.pipeline import compute_limit_signals
from app.services.huichun_data import HuichunDataReader
from app.strategy.first_board import (
    FirstBoardRules,
    filter_daily_pattern_history,
    required_history_bars,
)

_RAW = {"symbol", "date", "open", "high", "low", "close", "volume"}
_FACTOR_SCHEMA = {"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64}
_CANDIDATE_SCHEMA = {
    "symbol": pl.String, "date": pl.Date, "pattern": pl.String, "name": pl.String,
    "limit_up_price": pl.Float64, "exclude_reason": pl.String,
    "reference_date": pl.Date, "window_start": pl.Date, "exit_date": pl.Date,
}


def _unique(frame: pl.DataFrame, keys: list[str], label: str) -> None:
    if frame.select(pl.struct(keys).is_duplicated().any()).item():
        raise ValueError(f"duplicate {label}")


def _valid_raw(raw: pl.DataFrame) -> pl.DataFrame:
    prices = [pl.col(c).is_finite() & (pl.col(c) > 0)
              for c in ("open", "high", "low", "close", "volume")]
    valid = (pl.all_horizontal(prices)
             & (pl.col("high") >= pl.max_horizontal("open", "close", "low"))
             & (pl.col("low") <= pl.min_horizontal("open", "close", "high")))
    if "amount" in raw.columns:
        valid &= pl.col("amount").is_finite() & (pl.col("amount") > 0)
    complete = pl.lit(True)
    if "quote_ts" in raw.columns:
        midnight = (pl.col("date").cast(pl.Datetime("ms"))
                    .dt.replace_time_zone("Asia/Shanghai").dt.epoch("ms"))
        stamp = pl.col("quote_ts").cast(pl.Float64, strict=False)
        complete &= pl.col("quote_ts").is_null() | (
            stamp.is_finite() & (stamp >= midnight + 15 * 3_600_000)
            & (stamp < midnight + 24 * 3_600_000)
        )
    if "daily_batch_finalized" in raw.columns:
        complete &= pl.col("daily_batch_finalized").fill_null(True)
    return raw.with_columns(valid.fill_null(False).alias("_valid_price"),
                            complete.fill_null(False).alias("_complete"))


def prepare_first_board_ideal_candidates(
    raw: pl.DataFrame,
    factors: pl.DataFrame,
    instruments: pl.DataFrame,
    trading_days: list[date],
    *,
    pattern: str,
    params: dict,
    start: date,
    end: date,
) -> tuple[pl.DataFrame, dict]:
    """Map completed T-1 shapes to T and attach exact raw-price limit levels.

The independent calendar must include the history window and T+1 exit tail.
Return every detected shape with its exclusion reason (null means eligible).
Malformed identities/duplicates fail closed. Invalid bars are removed before
shape calculation and calendar continuity is checked again on the result.

Factor anomaly detection covers the supplied raw history only. Callers must
retain earlier event/reference rows to preserve known permanent quarantines;
the returned coverage dates make that limitation explicit.
"""
    # Reuse the daily strategy's strict parameter allow-list, including on empty input.
    filter_daily_pattern_history(pl.DataFrame(), params, pattern)
    rules = FirstBoardRules.model_validate({**params, "enabled_patterns": [pattern]})
    required = required_history_bars(rules)
    if (not trading_days or any(type(day) is not date for day in trading_days)
            or trading_days != sorted(set(trading_days))):
        raise ValueError("trading_days must be a sorted unique independent calendar")
    indices = {day: i for i, day in enumerate(trading_days)}
    if start > end or start not in indices or end not in indices:
        raise ValueError("study dates must be covered by the trading calendar")
    if indices[end] + 1 >= len(trading_days):
        raise ValueError("trading calendar must cover the next-session exit tail")
    tail = trading_days[indices[end] + 1]
    if not _RAW.issubset(raw.columns):
        raise ValueError(f"missing raw columns: {sorted(_RAW - set(raw.columns))}")
    raw = raw.with_columns(pl.col("date").cast(pl.Date, strict=True))
    if raw.select(pl.any_horizontal(pl.col("symbol").is_null(), pl.col("date").is_null()).any()).item():
        raise ValueError("raw symbol/date must be present")
    _unique(raw, ["symbol", "date"], "raw symbol/date")
    if raw.filter(~pl.col("date").is_in(trading_days)).height:
        raise ValueError("raw dates outside the independent trading calendar")
    raw = raw.filter(pl.col("date") <= tail).sort(["symbol", "date"])
    if factors.is_empty():
        factors = pl.DataFrame(schema=_FACTOR_SCHEMA)
    elif not set(_FACTOR_SCHEMA).issubset(factors.columns):
        raise ValueError("missing factor columns")
    else:
        factors = factors.select(*_FACTOR_SCHEMA).cast(_FACTOR_SCHEMA, strict=False)
    if factors["symbol"].null_count():
        raise ValueError("factor symbol must be present")
    _unique(factors, ["symbol", "trade_date"], "factor symbol/date")
    factors = factors.filter(pl.col("trade_date").is_null() | (pl.col("trade_date") <= tail))
    if "symbol" not in instruments.columns:
        raise ValueError("instrument symbol must be present")
    if instruments["symbol"].null_count():
        raise ValueError("instrument symbol must be present")
    _unique(instruments, ["symbol"], "instrument symbol")
    for field, dtype in (("name", pl.String), ("listing_date", pl.Date)):
        if field not in instruments.columns:
            instruments = instruments.with_columns(pl.lit(None, dtype=dtype).alias(field))
    metadata = instruments.select(
        "symbol", pl.col("name").cast(pl.String).str.strip_chars(),
        pl.col("listing_date").cast(pl.Date, strict=False),
    ).filter(pl.col("name").is_not_null() & (pl.col("name") != "")
             & pl.col("listing_date").is_not_null())
    marked = _valid_raw(raw)
    clean = marked.filter(pl.col("_valid_price") & pl.col("_complete"))
    # The anomaly checker needs event/predecessor prices and each symbol's
    # starting date, not a Python dictionary of the entire multi-year panel.
    check_keys = []
    for symbol, day, factor in factors.iter_rows():
        if day is not None and factor != 1:
            check_keys.append((symbol, day))
            if day in indices and indices[day] > 0:
                check_keys.append((symbol, trading_days[indices[day] - 1]))
    first_rows = clean.select("symbol", "date", "close").group_by("symbol", maintain_order=True).first()
    check_rows = clean.select("symbol", "date", "close").join(
        pl.DataFrame(check_keys, schema={"symbol": pl.String, "date": pl.Date}, orient="row").unique(),
        on=["symbol", "date"], how="semi",
    )
    failures = HuichunDataReader.factor_failure_dates(
        pl.concat([first_rows, check_rows]).unique(subset=["symbol", "date"]), factors, trading_days,
    )
    raw_dates = set(raw["date"].unique().to_list())
    raw_start = min(raw_dates) if raw_dates else None
    audit = {
        "current_metadata_only": True,
        "metadata_scope": "current_non_st_universe",
        "historical_st_verified": False,
        "price_basis": "raw_ohlc_event_free_windows",
        "factor_check_start": str(raw_start) if raw_start else None,
        "factor_check_end": str(max(raw_dates)) if raw_dates else None,
        "required_history_bars": required,
        "raw_rows": raw.height,
        "invalid_price_rows": marked.filter(~pl.col("_valid_price")).height,
        "incomplete_rows": marked.filter(~pl.col("_complete")).height,
        "missing_metadata_symbols": raw.select("symbol").unique().join(
            metadata.select("symbol"), on="symbol", how="anti").height,
        "factor_quarantine_from": {symbol: str(day) if day else None
                                   for symbol, day in sorted(failures.items())},
        "missing_market_days": [str(day) for day in trading_days
                                if raw_start and raw_start <= day <= tail and day not in raw_dates],
        "shape_candidates": 0, "eligible_candidates": 0, "excluded_candidates": 0,
        "exclusions": {},
    }
    history_start = trading_days[max(0, indices[start] - required)]
    clean = clean.filter(pl.col("date") >= history_start)
    clean = clean.join(metadata, on="symbol", how="inner").filter(
        pl.col("date") >= pl.col("listing_date")
    ).drop("name", "listing_date", "_valid_price", "_complete")
    if clean.is_empty():
        return pl.DataFrame(schema=_CANDIDATE_SCHEMA), audit
    # All OHLC share the raw basis. Windows containing any non-unit event are
    # excluded below, including the predecessor needed for historical limits.
    history = clean.with_columns(
        pl.col("close").alias("raw_close"), pl.col("high").alias("raw_high"),
        pl.col("low").alias("raw_low"),
    )
    history = compute_limit_signals(history, metadata, needed={"limit_up_price", "limit_down_price"})
    calendar = pl.DataFrame({"date": trading_days, "_calendar_index": range(len(trading_days))})
    history = history.join(metadata.select("symbol", "name"), on="symbol", how="left")
    history = history.join(calendar, on="date", how="left").sort(["symbol", "date"])
    history = history.with_columns(
        ((pl.col("_calendar_index") - pl.col("_calendar_index").shift(required - 1).over("symbol"))
         == required - 1).fill_null(False).alias("history_contiguous")
    )
    hits = filter_daily_pattern_history(history, params, pattern)
    hits = hits.filter(pl.col("date").is_between(trading_days[max(0, indices[start] - 1)],
                                                trading_days[indices[end] - 1]))
    facts = {(row["symbol"], row["date"]): row for row in history.select(
        "symbol", "date", "high", "low", "limit_up_price", "limit_down_price",
    ).filter(pl.col("date") >= start).iter_rows(named=True)}
    events: dict[str, list[date]] = {}
    for symbol, day, factor in factors.iter_rows():
        if day is not None and factor != 1:
            events.setdefault(symbol, []).append(day)
    for days in events.values():
        days.sort()
    rows = []
    for hit in hits.iter_rows(named=True):
        symbol, reference = hit["symbol"], hit["date"]
        buy_index = indices[reference] + 1
        if buy_index >= len(trading_days) or not start <= trading_days[buy_index] <= end:
            continue
        buy, exit_day = trading_days[buy_index], trading_days[buy_index + 1]
        window_start = trading_days[max(0, buy_index - required)]
        reason = None
        if symbol in failures and (failures[symbol] is None or failures[symbol] <= exit_day):
            reason = "factor_quarantine"
        elif not hit["history_contiguous"] or buy_index < required:
            reason = "history_gap"
        else:
            days = events.get(symbol, [])
            event_index = bisect_left(days, window_start)
            if event_index < len(days) and days[event_index] <= exit_day:
                reason = "corporate_action_window"
        entry = facts.get((symbol, buy))
        exit_bar = facts.get((symbol, exit_day))
        threshold = entry["limit_up_price"] if entry else None
        if reason is None:
            if (symbol, buy) not in facts:
                reason = "missing_entry_day"
            elif (symbol, exit_day) not in facts:
                reason = "missing_exit_day"
            elif any(bar[key] is None or bar[key] <= 0
                     for bar in (entry, exit_bar) for key in ("limit_up_price", "limit_down_price")):
                reason = "no_price_limit"
            elif any(bar["high"] > bar["limit_up_price"] + .005 + .01 + 1e-9
                     or bar["low"] < bar["limit_down_price"] - .005 - .01 - 1e-9
                     for bar in (entry, exit_bar)):
                reason = "daily_price_outside_limits"
        rows.append({
            "symbol": symbol, "date": buy, "pattern": pattern, "name": hit["name"],
            "limit_up_price": round(threshold + .005, 8) if threshold is not None else None,
            "exclude_reason": reason, "reference_date": reference,
            "window_start": window_start, "exit_date": exit_day,
        })
    candidates = pl.DataFrame(rows, schema=_CANDIDATE_SCHEMA).sort(["date", "symbol"])
    exclusions = Counter(row["exclude_reason"] for row in rows if row["exclude_reason"])
    audit.update(shape_candidates=len(rows), excluded_candidates=sum(exclusions.values()),
                 eligible_candidates=len(rows) - sum(exclusions.values()), exclusions=dict(exclusions))
    return candidates, audit
