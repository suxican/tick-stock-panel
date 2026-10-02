"""Reproducible Huichun A0 daily signal audit, without fills or return estimates.

Callers supply point-in-time, consistently adjusted positive closes. Unknown daily
gaps must split ``segment_id``; known suspensions need no fabricated bars. Missing
historical universe eligibility stays unknown, even when the price shape matches.
This research audit is not registered as an executable trading strategy.
"""

from dataclasses import dataclass
from datetime import date
from math import isfinite, ulp

import polars as pl


@dataclass(frozen=True)
class A0Params:
    warmup_bars: int = 250
    rally_threshold: float = 0.40
    zero_threshold: float = 0.02
    ma_window: int = 60
    slope_lag: int = 5

    def __post_init__(self):
        for name in ("warmup_bars", "ma_window", "slope_lag"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("rally_threshold", "zero_threshold"):
            value = getattr(self, name)
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")


@dataclass(frozen=True)
class A0AuditResult:
    cycles: pl.DataFrame
    signals: pl.DataFrame
    indicators: pl.DataFrame


_DEFAULT_PARAMS = A0Params()
_GROUP = ["symbol", "segment_id"]
CYCLE_SCHEMA = {
    "symbol": pl.String,
    "segment_id": pl.String,
    "g_date": pl.Date,
    "d_date": pl.Date,
    "r_date": pl.Date,
    "g_close": pl.Float64,
    "d_close": pl.Float64,
    "r_close": pl.Float64,
    "prior_bars_at_g": pl.UInt32,
    "rally_return": pl.Float64,
    "rally_qualified": pl.Boolean,
    "dif": pl.Float64,
    "dea": pl.Float64,
    "q": pl.Float64,
    "zero_distance": pl.Float64,
    "gap_distance": pl.Float64,
    "ma60": pl.Float64,
    "ma60_lag": pl.Float64,
    "shape_match": pl.Boolean,
    "eligible": pl.Boolean,
    "eligibility_reason": pl.String,
    "strict_signal": pl.Boolean,
    "status": pl.String,
    "reasons": pl.String,
    "audit_date": pl.Date,
    "last_observation_date": pl.Date,
}


def _prepare(frame: pl.DataFrame, end: date) -> pl.DataFrame:
    required = {"symbol", "date", "close"}
    if missing := required.difference(frame.columns):
        raise ValueError(f"missing required columns: {sorted(missing)}")
    frame = frame.with_columns(pl.col("date").cast(pl.Date, strict=True))
    if frame["date"].null_count():
        raise ValueError("date cannot be null")
    # Future prices, eligibility and gap annotations cannot change past results.
    frame = frame.filter(pl.col("date") <= end)
    defaults = {
        "segment_id": pl.lit("0"),
        "eligible": pl.lit(None, dtype=pl.Boolean),
        "eligibility_reason": pl.lit(
            "" if "eligible" in frame.columns else "historical_eligibility_not_provided"
        ),
        "segment_invalidated_at": pl.lit(None, dtype=pl.Date),
    }
    frame = frame.with_columns(
        [expr.alias(name) for name, expr in defaults.items() if name not in frame.columns]
    )
    if frame.schema["eligible"] not in (pl.Boolean, pl.Null):
        raise ValueError("eligible must be a nullable Boolean")
    frame = frame.select(
        pl.col("symbol").cast(pl.String),
        pl.col("date"),
        pl.col("close").cast(pl.Float64, strict=True),
        pl.col("segment_id").cast(pl.String),
        pl.col("eligible").cast(pl.Boolean),
        pl.col("eligibility_reason").cast(pl.String).fill_null(""),
        pl.col("segment_invalidated_at").cast(pl.Date, strict=True),
    ).sort(["symbol", "date"])
    if frame.filter(
        pl.col("symbol").is_null()
        | (pl.col("symbol") == "")
        | pl.col("segment_id").is_null()
        | pl.col("close").is_null()
        | ~pl.col("close").is_finite()
        | (pl.col("close") <= 0)
    ).height:
        raise ValueError("symbol/segment_id must be known; close must be finite and positive")
    if frame.select(pl.struct("symbol", "date").is_duplicated().any()).item():
        raise ValueError("duplicate symbol/date observations")
    runs = frame.filter(
        (pl.col("segment_id") != pl.col("segment_id").shift(1).over("symbol")).fill_null(True)
    )
    if runs.select(pl.struct(_GROUP).is_duplicated().any()).item():
        raise ValueError("each segment_id must describe one contiguous run per symbol")
    if frame.filter(
        pl.col("segment_invalidated_at").is_not_null()
        & (pl.col("segment_invalidated_at") <= pl.col("date"))
    ).height:
        raise ValueError("segment_invalidated_at must be later than its last valid observation")
    if frame.filter(
        pl.col("segment_invalidated_at").is_not_null()
        & (pl.col("date") != pl.col("date").max().over(_GROUP))
    ).height:
        raise ValueError("segment_invalidated_at belongs on the final row of its segment")
    return frame.with_columns(
        pl.when(pl.col("segment_invalidated_at") <= end)
        .then(pl.col("segment_invalidated_at"))
        .otherwise(None)
        .alias("segment_invalidated_at")
    )


def _add_crosses(frame: pl.DataFrame) -> pl.DataFrame:
    return (
        frame.with_columns(
            pl.when(pl.col("q") != 0)
            .then(pl.col("q").sign())
            .otherwise(None)
            .alias("_nonzero_sign")
        )
        .with_columns(
            pl.col("_nonzero_sign")
            .forward_fill()
            .shift(1)
            .over(_GROUP)
            .alias("previous_nonzero_sign")
        )
        .with_columns(
            ((pl.col("q") > 0) & (pl.col("previous_nonzero_sign") < 0))
            .fill_null(False)
            .alias("golden_cross"),
            ((pl.col("q") < 0) & (pl.col("previous_nonzero_sign") > 0))
            .fill_null(False)
            .alias("dead_cross"),
        )
        .drop("_nonzero_sign")
    )


def _indicators(frame: pl.DataFrame, params: A0Params) -> pl.DataFrame:
    frame = frame.with_columns(
        pl.col("close").ewm_mean(span=10, adjust=False).over(_GROUP).alias("ema10"),
        pl.col("close").ewm_mean(span=20, adjust=False).over(_GROUP).alias("ema20"),
        pl.col("close")
        .rolling_mean(window_size=params.ma_window, min_samples=params.ma_window)
        .over(_GROUP)
        .alias("ma60"),
        (pl.col("close").cum_count().over(_GROUP) - 1).alias("prior_bars"),
    ).with_columns((pl.col("ema10") - pl.col("ema20")).alias("dif"))
    frame = frame.with_columns(
        pl.col("dif").ewm_mean(span=9, adjust=False).over(_GROUP).alias("dea"),
        pl.col("ma60").shift(params.slope_lag).over(_GROUP).alias("ma60_lag"),
    ).with_columns((pl.col("dif") - pl.col("dea")).alias("q"))
    return _add_crosses(
        frame.with_columns(
            (pl.max_horizontal(pl.col("dif").abs(), pl.col("dea").abs()) / pl.col("close")).alias(
                "zero_distance"
            ),
            (pl.col("q").abs() / pl.col("close")).alias("gap_distance"),
        )
    )


def _at_least(value: float, threshold: float) -> bool:
    # A common adjustment scale and the threshold multiplication can each round
    # once. Two Float64 ULPs absorb those errors, not a financial-size tolerance.
    if not isfinite(value) or not isfinite(threshold):
        return value >= threshold
    return value >= threshold or threshold - value <= 2 * max(ulp(value), ulp(threshold))


def _cycle_row(g: dict, d: dict | None, r: dict | None, params: A0Params) -> dict:
    row = {name: None for name in CYCLE_SCHEMA}
    row.update(
        symbol=g["symbol"],
        segment_id=g["segment_id"],
        g_date=g["date"],
        g_close=g["close"],
        prior_bars_at_g=g["prior_bars"],
        reasons="",
        eligibility_reason="",
    )
    if d is not None:
        row.update(
            d_date=d["date"],
            d_close=d["close"],
            rally_return=d["close"] / g["close"] - 1,
            rally_qualified=_at_least(d["close"], g["close"] * (1 + params.rally_threshold)),
        )
    if r is None:
        return row
    row.update(
        r_date=r["date"],
        r_close=r["close"],
        eligible=r["eligible"],
        eligibility_reason=r["eligibility_reason"],
        audit_date=r["date"],
        last_observation_date=r["date"],
    )
    for key in ("dif", "dea", "q", "zero_distance", "gap_distance", "ma60", "ma60_lag"):
        row[key] = r[key]
    reasons = []
    if not row["rally_qualified"]:
        reasons.append("rally_below_threshold")
    if any(r[key] is None for key in ("zero_distance", "ma60", "ma60_lag")):
        reasons.append("indicator_history_insufficient")
    else:
        if not _at_least(params.zero_threshold, r["zero_distance"]):
            reasons.append("zero_distance_exceeded")
        if r["close"] <= r["ma60"]:
            reasons.append("close_not_above_ma")
        if r["ma60"] <= r["ma60_lag"]:
            reasons.append("ma_not_rising")
    row["shape_match"] = not reasons
    if reasons:
        row["status"] = "first_cross_rejected" if row["rally_qualified"] else "rally_rejected"
        row["strict_signal"] = False
    elif r["eligible"] is None:
        row["status"] = "eligibility_unknown"
        reasons.append("historical_eligibility_unknown")
    elif r["eligible"] is False:
        row["status"] = "ineligible"
        row["strict_signal"] = False
        reasons.append("historical_universe_excluded")
    else:
        row["status"] = "signal"
        row["strict_signal"] = True
    row["reasons"] = ";".join(reasons)
    return row


def _cycles(indicators: pl.DataFrame, params: A0Params, end: date) -> pl.DataFrame:
    if indicators.is_empty():
        return pl.DataFrame(schema=CYCLE_SCHEMA)
    # Only crossings enter the Python state machine; EMA/MA stay columnar.
    metadata = (
        indicators.group_by(_GROUP, maintain_order=True)
        .agg(
            pl.col("date").first().alias("first_date"),
            pl.col("date").last().alias("last_date"),
            pl.col("segment_invalidated_at").last(),
        )
        .with_columns(pl.col("first_date").shift(-1).over("symbol").alias("next_segment_date"))
    )
    crosses = indicators.filter(pl.col("golden_cross") | pl.col("dead_cross"))
    groups = crosses.partition_by(_GROUP, maintain_order=True, as_dict=True)
    rows = []
    for meta in metadata.iter_rows(named=True):
        events = groups.get((meta["symbol"], meta["segment_id"]))
        if events is None:
            continue
        g, d = None, None
        for event in events.iter_rows(named=True):
            if event["golden_cross"]:
                if g is not None and d is not None:
                    rows.append(_cycle_row(g, d, event, params))
                g = event if event["prior_bars"] >= params.warmup_bars else None
                d = None
            elif g is not None and d is None:
                d = event
        if g is None:
            continue
        row = _cycle_row(g, d, None, params)
        invalidated = meta["segment_invalidated_at"] or meta["next_segment_date"]
        if invalidated is not None:
            row.update(
                status="cycle_unknown_gap", reasons="unknown_daily_gap", audit_date=invalidated
            )
        elif d is None:
            row.update(
                status="waiting_dead_cross", reasons="period_end_before_dead_cross", audit_date=end
            )
        elif row["rally_qualified"]:
            row.update(
                status="waiting_first_cross",
                reasons="period_end_before_first_cross",
                audit_date=end,
            )
        else:
            row.update(
                status="rally_rejected",
                reasons="rally_below_threshold",
                shape_match=False,
                strict_signal=False,
                audit_date=d["date"],
            )
        row["last_observation_date"] = meta["last_date"]
        rows.append(row)
    return pl.DataFrame(rows, schema=CYCLE_SCHEMA).sort(["symbol", "g_date"])


def audit_a0(
    frame: pl.DataFrame,
    *,
    start: date,
    end: date,
    params: A0Params = _DEFAULT_PARAMS,
) -> A0AuditResult:
    """Audit the supplied history through ``end``; ``start`` only clips outputs.

    ``signals`` contains all matching shapes, including excluded/unknown historical
    eligibility. Only ``strict_signal is True`` is a confirmed eligible A0 signal.
    ``segment_invalidated_at`` optionally marks the first unknown missing date on
    each segment's last valid row, including trailing gaps without a following row.
    No history, universe status, calendar or corporate action is inferred here.
    MA columns retain the specification's ``ma60`` names if research parameters
    override the window; callers must record the full parameter snapshot.
    """
    if start > end:
        raise ValueError("start must not be after end")
    prepared = _prepare(frame, end)
    indicators = _indicators(prepared, params)
    cycles = _cycles(indicators, params, end).filter(pl.col("audit_date") >= start)
    signals = cycles.filter(pl.col("shape_match").fill_null(False))
    return A0AuditResult(
        cycles=cycles,
        signals=signals,
        indicators=indicators.filter(pl.col("date") >= start),
    )
