"""Price-path research for Huichun signals, separate from executable returns.

Returns and excursions are fractions: 0.10 means 10%. Paths use the supplied
market calendar and one consistent adjusted-price scale. Missing observations
remain in the denominator; there is no price filling or network access.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date

import numpy as np
import polars as pl

EVENT_SCHEMA = {
    "symbol": pl.String,
    "signal_date": pl.Date,
    "horizon": pl.Int64,
    "target_date": pl.Date,
    "status": pl.String,
    "return_pct": pl.Float64,
    "mfe": pl.Float64,
    "mae": pl.Float64,
}
BOOTSTRAP_SEED = 20261002
BOOTSTRAP_SAMPLES = 2000
_STATUSES = {"ok", "unknown_gap", "censored", "missing_signal_price"}


def _positive(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def event_study(
    signals: pl.DataFrame,
    daily: pl.DataFrame,
    market_dates: list[date],
    *,
    end: date,
    horizons: tuple[int, ...] = (1, 3, 5, 10, 20),
) -> pl.DataFrame:
    """Keep one result for each input signal and requested market-day horizon.

    A missing/invalid signal close takes precedence over censoring. If the full
    horizon lies beyond ``end`` or the calendar, it is censored. Otherwise any
    missing, duplicate, or invalid future bar makes the entire path unknown.
    Signal-day highs/lows never enter excursions. MFE/MAE include the zero
    baseline, so they are respectively nonnegative and nonpositive.
    """
    if not {"symbol", "r_date"}.issubset(signals.columns):
        raise ValueError("signals requires symbol and r_date")
    required = {"symbol", "date", "adj_close", "adj_high", "adj_low"}
    if not required.issubset(daily.columns):
        raise ValueError(f"daily requires {sorted(required)}")
    if not horizons or any(type(h) is not int or h < 1 for h in horizons):
        raise ValueError("horizons must contain positive integers")
    if len(set(horizons)) != len(horizons):
        raise ValueError("horizons must not contain duplicates")
    calendar = sorted(set(market_dates))
    index = {day: i for i, day in enumerate(calendar)}
    signal_rows = list(signals.select("symbol", "r_date").iter_rows())
    if not signal_rows:
        return pl.DataFrame(schema=EVENT_SCHEMA)
    needed = set()
    for symbol, signal_date in signal_rows:
        if not isinstance(symbol, str) or not symbol or not isinstance(signal_date, date):
            raise ValueError("signal symbol and r_date must be non-null")
        needed.add((symbol, signal_date))
        start = index.get(signal_date)
        if start is not None:
            needed.update(
                (symbol, day)
                for day in calendar[start + 1 : start + max(horizons) + 1]
                if day <= end
            )
    requested = pl.DataFrame(
        sorted(needed), schema={"symbol": pl.String, "date": pl.Date}, orient="row"
    )
    # Keep only requested event paths in Python memory, not the entire daily store.
    selected = daily.filter(pl.col("date") <= end).join(
        requested, on=["symbol", "date"], how="semi"
    )
    prices: dict[tuple[str, date], tuple | None] = {}
    for symbol, day, close, high, low in selected.select(
        "symbol", "date", "adj_close", "adj_high", "adj_low"
    ).iter_rows():
        if day is None or day > end:
            continue
        key = (symbol, day)
        prices[key] = None if key in prices else (close, high, low)

    rows = []
    for symbol, signal_date in signal_rows:
        signal_index = index.get(signal_date)
        origin = prices.get((symbol, signal_date))
        for horizon in horizons:
            target_index = signal_index + horizon if signal_index is not None else None
            target = (
                calendar[target_index]
                if target_index is not None and target_index < len(calendar)
                else None
            )
            row = {
                "symbol": symbol,
                "signal_date": signal_date,
                "horizon": horizon,
                "target_date": target,
                "status": "ok",
                "return_pct": None,
                "mfe": None,
                "mae": None,
            }
            if origin is None or not _positive(origin[0]):
                row["status"] = "missing_signal_price"
            elif signal_index is None:
                raise ValueError(f"Signal date absent from market calendar: {signal_date}")
            elif target is None or target > end:
                row["status"] = "censored"
            else:
                path = [
                    prices.get((symbol, day))
                    for day in calendar[signal_index + 1 : target_index + 1]
                ]
                if any(
                    bar is None
                    or not all(_positive(value) for value in bar)
                    or not bar[2] <= bar[0] <= bar[1]
                    for bar in path
                ):
                    row["status"] = "unknown_gap"
                else:
                    close = origin[0]
                    row["return_pct"] = path[-1][0] / close - 1
                    row["mfe"] = max(0.0, max(bar[1] for bar in path) / close - 1)
                    row["mae"] = min(0.0, min(bar[2] for bar in path) / close - 1)
            rows.append(row)
    return pl.DataFrame(rows, schema=EVENT_SCHEMA)


def summarize_events(events: pl.DataFrame) -> list[dict]:
    """Summarize each horizon and bootstrap whole signal-date clusters.

    Each draw samples dates with replacement and keeps every valid event from
    a sampled date together. The resulting mean remains event-weighted, not a
    mean of daily means. Fewer than two valid dates gives no confidence interval.
    Counts include all signals; return statistics use only valid observations.
    The percentile interval does not address serial dependence between dates.
    """
    if not set(EVENT_SCHEMA).issubset(events.columns):
        raise ValueError("events does not match event_study schema")
    if events.is_empty():
        return []
    if set(events["status"].to_list()) - _STATUSES:
        raise ValueError("Unknown event status")
    summaries = []
    for key, group in sorted(events.partition_by("horizon", as_dict=True).items()):
        valid = group.filter(pl.col("status") == "ok").sort(["signal_date", "symbol"])
        for column in ("return_pct", "mfe", "mae"):
            if valid.filter(pl.col(column).is_null() | ~pl.col(column).is_finite()).height:
                raise ValueError("Valid events require finite returns and excursions")
        values = valid["return_pct"].to_numpy()
        clusters: dict[date, list[float]] = defaultdict(list)
        for day, value in valid.select("signal_date", "return_pct").iter_rows():
            if day is None:
                raise ValueError("Valid events require signal_date")
            clusters[day].append(value)
        low = high = None
        if len(clusters) >= 2:
            cluster_values = [clusters[day] for day in sorted(clusters)]
            sums = np.array([sum(items) for items in cluster_values], dtype=np.float64)
            counts = np.array([len(items) for items in cluster_values], dtype=np.int64)
            rng = np.random.default_rng(BOOTSTRAP_SEED)
            draws = rng.integers(0, len(clusters), size=(BOOTSTRAP_SAMPLES, len(clusters)))
            means = sums[draws].sum(axis=1) / counts[draws].sum(axis=1)
            low, high = (float(value) for value in np.quantile(means, [0.025, 0.975]))
        counts = group.group_by("status").len()
        status_counts = dict(counts.iter_rows())
        missing_start = status_counts.get("missing_signal_price", 0)
        unknown_gap = status_counts.get("unknown_gap", 0)
        summaries.append(
            {
                "horizon": int(key[0]),
                "total": group.height,
                "valid": valid.height,
                "missing": missing_start + unknown_gap,
                "missing_signal_price": missing_start,
                "unknown_gap": unknown_gap,
                "censored": status_counts.get("censored", 0),
                "mean_return": float(np.mean(values)) if values.size else None,
                "median_return": float(np.median(values)) if values.size else None,
                "win_rate": float(np.mean(values > 0)) if values.size else None,
                "mean_mfe": float(valid["mfe"].mean()) if values.size else None,
                "mean_mae": float(valid["mae"].mean()) if values.size else None,
                "mean_return_ci95_low": low,
                "mean_return_ci95_high": high,
                "valid_signal_dates": len(clusters),
                "bootstrap_seed": BOOTSTRAP_SEED,
                "bootstrap_samples": BOOTSTRAP_SAMPLES,
                "bootstrap_unit": "signal_date",
            }
        )
    return summaries
