"""Daily pending-cross observations built from the existing A0 audit result.

The audit must include history through the requested cutoff. No indicator is
recomputed, and a consumed first cross can never reopen its previous cycle.
These observations are not confirmed signals or executable entry instructions.
"""

from datetime import date

import polars as pl

from app.strategy.huichun_a0 import A0AuditResult, A0Params, _at_least, pending_shape

WATCH_SCHEMA = {
    "symbol": pl.String,
    "observation_date": pl.Date,
    "g_date": pl.Date,
    "d_date": pl.Date,
    "rally_return": pl.Float64,
    "zero_distance": pl.Float64,
    "gap_distance": pl.Float64,
    "previous_gap_distance": pl.Float64,
    "dif": pl.Float64,
    "dea": pl.Float64,
    "q": pl.Float64,
    "previous_q": pl.Float64,
    "close_above_ma_pct": pl.Float64,
    "ma_slope_pct": pl.Float64,
    "adjusted_close": pl.Float64,
    "first_watch_date": pl.Date,
    "cross_threshold_return": pl.Float64,
    "threshold_zero_pass": pl.Boolean,
    "threshold_trend_pass": pl.Boolean,
}


def new_conversion_counts() -> list[dict[str, int]]:
    return [
        dict(horizon=h, total=0, resolved=0, confirmed=0, rejected=0, pending=0, gap_unknown=0)
        for h in (1, 3, 5)
    ]


def update_conversion_counts(counts: list[dict[str, int]], cycles: pl.DataFrame, start: date) -> None:
    """One entry per first watch within the scan window. All distances are valid bars."""
    observed = cycles.filter(pl.col("first_watch_date") >= start).select(
        "status", "shape_match", "watch_elapsed_bars", "watch_lead_bars",
    )
    for status, matched, elapsed, lead in observed.iter_rows():
        for row in counts:
            horizon = row["horizon"]
            row["total"] += 1
            if lead is not None or elapsed >= horizon:
                row["resolved"] += 1
                if lead is not None and lead <= horizon:
                    row["confirmed" if matched else "rejected"] += 1
            elif status == "cycle_unknown_gap":
                row["gap_unknown"] += 1
            else:
                row["pending"] += 1


def pending_crosses(
    audit: A0AuditResult, *, observation_date: date, params: A0Params,
) -> pl.DataFrame:
    """Require a still-open qualified cycle and strictly converging negative q.

    The preceding bar must belong to the same uninterrupted segment and must
    not precede its dead cross. Missing cutoff bars never use an older quote.
    Distances and returns use decimal ratios; all indicator prices are adjusted.
    """
    active = audit.cycles.filter(
        (pl.col("status") == "waiting_first_cross") & pl.col("rally_qualified")
    ).select("symbol", "segment_id", "g_date", "d_date", "rally_return", "first_watch_date")
    if active.is_empty() or audit.indicators.is_empty():
        return pl.DataFrame(schema=WATCH_SCHEMA)
    group = ["symbol", "segment_id"]
    terminal = (
        audit.indicators.with_columns(
            pl.col("q").shift(1).over(group).alias("previous_q"),
            pl.col("gap_distance").shift(1).over(group).alias("previous_gap_distance"),
            pl.col("close").shift(params.ma_window - 1).over(group).alias("_outgoing_close"),
            pl.col("ma60").shift(params.slope_lag - 1).over(group).alias("_next_ma_lag"),
        )
        .filter(pl.col("date") == observation_date)
        .join(active, on=group, how="inner")
        .filter(pending_shape(params))
    )
    if terminal.is_empty():
        return pl.DataFrame(schema=WATCH_SCHEMA)
    # Solving next DIF = current DEA gives q_next=0. The strict cross requires
    # a higher close. This equivalent EMA10-based form avoids subtracting two large EMAs.
    fast, slow = 2 / 11, 2 / 21
    threshold = pl.col("ema10") + (pl.col("dea") - (1 - slow) * pl.col("dif")) / (fast - slow)
    terminal = terminal.with_columns(
        pl.when(threshold.is_finite() & (threshold > 0)).then(threshold).alias("_threshold")
    ).with_columns(
        (pl.col("ma60") + (pl.col("_threshold") - pl.col("_outgoing_close")) / params.ma_window)
        .alias("_next_ma"),
    ).with_columns(
        (pl.col("_threshold") / pl.col("close") - 1).alias("cross_threshold_return"),
        (pl.col("dea").abs() / pl.col("_threshold")).map_elements(
            lambda value: _at_least(params.zero_threshold, value), return_dtype=pl.Boolean,
        ).alias("threshold_zero_pass"),
        ((pl.col("_threshold") > pl.col("_next_ma")) & (pl.col("_next_ma") > pl.col("_next_ma_lag")))
        .alias("threshold_trend_pass"),
        pl.col("date").alias("observation_date"),
        pl.col("close").alias("adjusted_close"),
        (pl.col("close") / pl.col("ma60") - 1).alias("close_above_ma_pct"),
        (pl.col("ma60") / pl.col("ma60_lag") - 1).alias("ma_slope_pct"),
    )
    return terminal.select(*WATCH_SCHEMA).sort(["gap_distance", "symbol"])
