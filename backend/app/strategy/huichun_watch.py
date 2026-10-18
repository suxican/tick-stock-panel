"""Daily pending-cross observations built from the existing A0 audit result.

The audit must include history through the requested cutoff. No indicator is
recomputed, and a consumed first cross can never reopen its previous cycle.
These observations are not confirmed signals or executable entry instructions.
"""

from datetime import date

import polars as pl

from app.strategy.huichun_a0 import A0AuditResult, A0Params, _at_least

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
}


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
    ).select("symbol", "segment_id", "g_date", "d_date", "rally_return")
    if active.is_empty() or audit.indicators.is_empty():
        return pl.DataFrame(schema=WATCH_SCHEMA)
    group = ["symbol", "segment_id"]
    terminal = (
        audit.indicators.with_columns(
            pl.col("date").shift(1).over(group).alias("previous_date"),
            pl.col("q").shift(1).over(group).alias("previous_q"),
            pl.col("gap_distance").shift(1).over(group).alias("previous_gap_distance"),
        )
        .filter(pl.col("date") == observation_date)
        .join(active, on=group, how="inner")
        .filter(
            (pl.col("previous_date") >= pl.col("d_date"))
            & (pl.col("previous_q") < pl.col("q"))
            & (pl.col("q") < 0)
            & (pl.col("close") > pl.col("ma60"))
            & (pl.col("ma60") > pl.col("ma60_lag"))
            & (pl.col("ma60_lag") > 0)
        )
    )
    if terminal.is_empty():
        return pl.DataFrame(schema=WATCH_SCHEMA)
    # Only one terminal row per stock reaches the shared Float64 boundary gate.
    terminal = terminal.filter(
        pl.col("zero_distance").map_elements(
            lambda value: _at_least(params.zero_threshold, value), return_dtype=pl.Boolean,
        )
    ).with_columns(
        pl.col("date").alias("observation_date"),
        pl.col("close").alias("adjusted_close"),
        (pl.col("close") / pl.col("ma60") - 1).alias("close_above_ma_pct"),
        (pl.col("ma60") / pl.col("ma60_lag") - 1).alias("ma_slope_pct"),
    )
    return terminal.select(*WATCH_SCHEMA).sort(["gap_distance", "symbol"])
