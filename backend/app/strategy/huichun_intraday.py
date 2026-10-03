"""Pure A1/B1 research signals using A0's daily history and cycle definitions.

Minute intervals are explicit, left-closed/right-open, Shanghai naive datetimes.
Volume is shares. Day metadata is point-in-time at 10:30, never inferred from
current names or end-of-day status. Missing metadata stays unknown. Candidates
retain unknown historical eligibility; only strict_signal=True verifies it.
No account, order, execution-window volume or future outcome enters this module.
"""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from math import isfinite

import polars as pl

from app.strategy.huichun_a0 import A0Params, _at_least, audit_a0

MINUTE_SCHEMA = {
    "symbol": pl.String,
    "interval_start": pl.Datetime("us"),
    "interval_end": pl.Datetime("us"),
    "close": pl.Float64,
    "volume_shares": pl.Float64,
}
METADATA_SCHEMA = {
    "symbol": pl.String,
    "date": pl.Date,
    "auction_excluded": pl.Boolean,
    "volume_comparable": pl.Boolean,
    "price_scale": pl.Float64,
    "eligible": pl.Boolean,
}
OBSERVATION_SCHEMA = {
    "version": pl.String,
    "symbol": pl.String,
    "g_date": pl.Date,
    "d_date": pl.Date,
    "signal_time": pl.Datetime("us"),
    "data_cutoff": pl.Datetime("us"),
    "reference_price": pl.Float64,
    "raw_price": pl.Float64,
    "rally_return": pl.Float64,
    "dif": pl.Float64,
    "dea": pl.Float64,
    "q": pl.Float64,
    "zero_distance": pl.Float64,
    "gap_distance": pl.Float64,
    "ma60": pl.Float64,
    "ma60_lag": pl.Float64,
    "volume_ratio": pl.Float64,
    "shape_match": pl.Boolean,
    "eligible": pl.Boolean,
    "strict_signal": pl.Boolean,
    "status": pl.String,
    "reason": pl.String,
}
INTRADAY_CYCLE_SCHEMA = {
    "version": pl.String,
    "symbol": pl.String,
    "g_date": pl.Date,
    "d_date": pl.Date,
    "r_date": pl.Date,
    "status": pl.String,
    "signal_time": pl.Datetime("us"),
}


@dataclass(frozen=True)
class IntradayParams:
    volume_ratio: float = 1.5
    gap_threshold: float = 0.001

    def __post_init__(self):
        if any(not isfinite(v) or v <= 0 for v in (self.volume_ratio, self.gap_threshold)):
            raise ValueError("Intraday thresholds must be positive and finite")


_DEFAULT_A0_PARAMS = A0Params()
_DEFAULT_INTRADAY_PARAMS = IntradayParams()


@dataclass(frozen=True)
class MorningObservation:
    status: str
    reason: str = ""
    volume_ratio: float | None = None
    raw_price: float | None = None
    volume_shares: float | None = None
    reference_volume: float | None = None


@dataclass(frozen=True)
class IntradayAuditResult:
    observations: pl.DataFrame
    signals: pl.DataFrame
    cycles: pl.DataFrame


def morning_observation(
    minutes: pl.DataFrame,
    *,
    symbol: str,
    day: date,
    market_dates: list[date],
    auction_excluded: dict[date, bool | None],
    volume_comparable: dict[date, bool | None],
    require_last_trade: bool = True,
) -> MorningObservation:
    """Use exactly this day and its previous three market dates, without backfill.

    A bar ending at 10:30 represents trades strictly before 10:30 and is allowed;
    a bar starting at 10:30 is excluded. The last allowed bar needs real trades.
    Known halts or share splits set volume_comparable=False (deterministic skip).
    Unknown comparability or inseparable auction volume makes the result unknown.
    """
    calendar = sorted(set(market_dates))
    index = bisect_left(calendar, day)
    if index == len(calendar) or calendar[index] != day or index < 3:
        return MorningObservation("unknown", "three_market_days_unavailable")
    needed = calendar[index - 3 : index + 1]
    if any(volume_comparable.get(d) is False for d in needed):
        return MorningObservation("excluded", "known_noncomparable_volume")
    if any(volume_comparable.get(d) is not True for d in needed):
        return MorningObservation("unknown", "volume_comparability_unknown")
    if any(auction_excluded.get(d) is not True for d in needed):
        return MorningObservation("unknown", "auction_volume_not_separable")
    if minutes.is_empty():
        return MorningObservation("unknown", "morning_minutes_missing")
    if set(MINUTE_SCHEMA) - set(minutes.columns):
        return MorningObservation("unknown", "minute_interval_or_unit_contract_missing")
    for column in ("interval_start", "interval_end"):
        dtype = minutes.schema[column]
        if not isinstance(dtype, pl.Datetime) or dtype.time_zone is not None:
            return MorningObservation("unknown", "minute_timezone_or_label_unknown")
    volumes = []
    raw_price = None
    for observed_day in needed:
        begin = datetime.combine(observed_day, time(9, 30))
        cutoff = datetime.combine(observed_day, time(10, 30))
        selected = minutes.filter(
            (pl.col("symbol") == symbol)
            & (pl.col("interval_start") >= begin)
            & (pl.col("interval_start") < cutoff)
        ).sort("interval_start")
        expected = [begin + timedelta(minutes=i) for i in range(60)]
        if selected.height != 60 or selected["interval_start"].to_list() != expected:
            return MorningObservation("unknown", "morning_minutes_missing_or_duplicated")
        if selected["interval_end"].to_list() != [t + timedelta(minutes=1) for t in expected]:
            return MorningObservation("unknown", "minute_interval_not_one_completed_minute")
        if selected.filter(
            pl.col("volume_shares").is_null()
            | ~pl.col("volume_shares").is_finite()
            | (pl.col("volume_shares") < 0)
        ).height:
            return MorningObservation("unknown", "minute_volume_invalid")
        volumes.append(float(selected["volume_shares"].sum()))
        if observed_day == day:
            last = selected.row(-1, named=True)
            if require_last_trade and last["volume_shares"] <= 0:
                return MorningObservation("excluded", "last_completed_minute_has_no_trade")
            raw_price = last["close"]
            if require_last_trade and (
                raw_price is None or not isfinite(raw_price) or raw_price <= 0
            ):
                return MorningObservation("unknown", "last_trade_price_missing")
    reference = sum(volumes[:3]) / 3
    if reference <= 0:
        return MorningObservation("excluded", "reference_volume_not_positive")
    return MorningObservation("ok", "", volumes[3] / reference, raw_price, volumes[3], reference)


def temporary_daily_indicators(
    history: pl.DataFrame, adjusted_price: float, *, params: A0Params = _DEFAULT_A0_PARAMS
) -> dict:
    """Append one temporary close to A0 EMA state, without mutating daily history."""
    if not isfinite(adjusted_price) or adjusted_price <= 0:
        raise ValueError("adjusted_price must be finite and positive")
    for column in ("symbol", "segment_id"):
        if column in history.columns and history[column].n_unique() != 1:
            raise ValueError("Temporary daily state must belong to one symbol and segment")
    required = {"close", "ema10", "ema20", "dea", "q", "ma60"}
    if required - set(history.columns) or history.height < max(
        params.ma_window - 1, params.slope_lag, 2
    ):
        raise ValueError("Insufficient A0 indicator history for temporary daily bar")
    previous = history.row(-1, named=True)
    ema10 = 2 / 11 * adjusted_price + 9 / 11 * previous["ema10"]
    ema20 = 2 / 21 * adjusted_price + 19 / 21 * previous["ema20"]
    dif = ema10 - ema20
    dea = 0.2 * dif + 0.8 * previous["dea"]
    prior_sum = history["close"].tail(params.ma_window - 1).sum() if params.ma_window > 1 else 0
    return {
        "ema10": ema10,
        "ema20": ema20,
        "dif": dif,
        "dea": dea,
        "q": dif - dea,
        "ma60": (prior_sum + adjusted_price) / params.ma_window,
        "ma60_lag": history["ma60"][-params.slope_lag],
        "zero_distance": max(abs(dif), abs(dea)) / adjusted_price,
        "gap_distance": abs(dif - dea) / adjusted_price,
    }


def _row(version, cycle, day):
    stamp = datetime.combine(day, time(10, 30) if version == "B1" else time(15))
    row = dict.fromkeys(OBSERVATION_SCHEMA)
    row.update(
        version=version,
        symbol=cycle["symbol"],
        g_date=cycle["g_date"],
        d_date=cycle["d_date"],
        rally_return=cycle["rally_return"],
        signal_time=stamp,
        data_cutoff=stamp,
        shape_match=False,
        strict_signal=False,
        status="rejected",
        reason="",
    )
    return row


def _match(row):
    row.update(shape_match=True, strict_signal=row["eligible"])
    row["status"] = "signal" if row["eligible"] is True else "eligibility_unknown"
    row["reason"] = "" if row["eligible"] is True else "historical_eligibility_unknown"


def audit_intraday(
    daily: pl.DataFrame,
    minutes: pl.DataFrame,
    *,
    market_dates: list[date],
    start: date,
    end: date,
    day_metadata: pl.DataFrame | None = None,
    params: A0Params = _DEFAULT_A0_PARAMS,
    intraday_params: IntradayParams = _DEFAULT_INTRADAY_PARAMS,
) -> IntradayAuditResult:
    """Scan A1/B1 independently; start clips output, never resets earlier chances.

    ``daily`` has the same causal price basis and gap annotations as audit_a0.
    Unlike A0's close-only scan, each segment followed by another segment must
    mark its first unknown day with ``segment_invalidated_at`` on its last row.
    The next segment's start cannot locate that gap for a 10:30 observation.
    ``day_metadata`` follows METADATA_SCHEMA. price_scale converts current raw
    price onto that fixed daily basis; its absence prevents a B1 price test.
    Metadata for every relevant day must already be known by that day's 10:30.
    Unknown eligibility preserves research candidates, while known exclusion
    suppresses them. Unknown potential first-minute observations end B1's cycle.
    """
    if start > end:
        raise ValueError("start must not be after end")
    if daily.is_empty():
        return IntradayAuditResult(
            pl.DataFrame(schema=OBSERVATION_SCHEMA),
            pl.DataFrame(schema=OBSERVATION_SCHEMA),
            pl.DataFrame(schema=INTRADAY_CYCLE_SCHEMA),
        )
    calendar = sorted({d for d in market_dates if d <= end})
    earliest = daily["date"].min()
    a0 = audit_a0(daily, start=earliest, end=end, params=params)
    if set(a0.indicators["date"].to_list()) - set(calendar):
        raise ValueError(
            "market_dates must cover the full daily history used to restore opportunities"
        )
    metadata = day_metadata if day_metadata is not None else pl.DataFrame(schema=METADATA_SCHEMA)
    if {"symbol", "date"} - set(metadata.columns):
        raise ValueError("day_metadata requires symbol/date keys")
    metadata = metadata.filter(pl.col("date") <= end)
    if metadata.select(pl.struct("symbol", "date").is_duplicated().any()).item():
        raise ValueError("duplicate day_metadata keys")
    for column in ("eligible", "auction_excluded", "volume_comparable"):
        if column in metadata.columns and metadata.schema[column] not in (pl.Boolean, pl.Null):
            raise ValueError(f"{column} must be a nullable Boolean")
    meta = {(r["symbol"], r["date"]): r for r in metadata.iter_rows(named=True)}
    stock_metadata = {}
    for (symbol, day), record in meta.items():
        stock_metadata.setdefault(symbol, {})[day] = record
    stock_minutes = (
        minutes.partition_by("symbol", as_dict=True)
        if not minutes.is_empty() and "symbol" in minutes.columns
        else {}
    )
    groups = a0.indicators.partition_by(["symbol", "segment_id"], as_dict=True)
    last_segments = dict(
        a0.indicators.group_by("symbol").agg(pl.col("segment_id").last()).iter_rows()
    )
    for (symbol, segment_id), history in groups.items():
        if segment_id != last_segments[symbol] and history["segment_invalidated_at"][-1] is None:
            raise ValueError(
                "segment_invalidated_at must identify the first unknown date before "
                f"the next segment: {symbol}/{segment_id}"
            )
    morning_cache = {}

    def morning(symbol, day, version):
        key = symbol, day, version
        if key not in morning_cache:
            stock_meta = stock_metadata.get(symbol, {})
            morning_cache[key] = morning_observation(
                stock_minutes.get((symbol,), minutes.head(0)),
                symbol=symbol,
                day=day,
                market_dates=calendar,
                auction_excluded={d: r.get("auction_excluded") for d, r in stock_meta.items()},
                volume_comparable={d: r.get("volume_comparable") for d, r in stock_meta.items()},
                require_last_trade=version == "B1",
            )
        return morning_cache[key]

    observations, cycle_rows = [], []
    for cycle in a0.cycles.iter_rows(named=True):
        if cycle["rally_qualified"] is not True:
            continue
        symbol, r_date = cycle["symbol"], cycle["r_date"]
        history = groups[(symbol, cycle["segment_id"])]
        history_dates = history["date"].to_list()
        for version in ("A1", "B1"):
            state = "waiting_first_cross"
            signaled_at = None
            if version == "A1":
                observe_days = [r_date] if r_date is not None else []
            else:
                limit = r_date or cycle["audit_date"] or end
                observe_days = calendar[
                    bisect_right(calendar, cycle["d_date"]) : bisect_right(calendar, limit)
                ]
            for day in observe_days:
                row = _row(version, cycle, day)
                if version == "A1":
                    row["eligible"] = cycle["eligible"]
                    row["reference_price"] = cycle["r_close"]
                    for key in (
                        "dif",
                        "dea",
                        "q",
                        "zero_distance",
                        "gap_distance",
                        "ma60",
                        "ma60_lag",
                    ):
                        row[key] = cycle[key]
                    if cycle["shape_match"] is not True:
                        row["reason"] = "first_cross_shape_rejected"
                        observations.append(row)
                        state = "first_cross_rejected"
                        break
                else:
                    row["eligible"] = meta.get((symbol, day), {}).get("eligible")
                    prior = history.head(bisect_left(history_dates, day))
                    if prior.height < 2 or not (prior["q"][-2] < prior["q"][-1] < 0):
                        row["reason"] = "daily_gap_not_converging"
                        observations.append(row)
                        continue
                if row["eligible"] is False:
                    row.update(status="ineligible", reason="historical_universe_excluded")
                    observations.append(row)
                    if version == "A1":
                        state = "first_cross_rejected"
                    continue
                observed = morning(symbol, day, version)
                if observed.status != "ok":
                    row.update(status=observed.status, reason=observed.reason)
                    observations.append(row)
                    if observed.status == "unknown":
                        row.update(shape_match=None, strict_signal=None)
                        state = "first_signal_unknown" if version == "B1" else "observation_unknown"
                        break
                    if version == "A1":
                        state = "first_cross_rejected"
                    continue
                row["volume_ratio"] = observed.volume_ratio
                if not _at_least(observed.volume_ratio, intraday_params.volume_ratio):
                    row["reason"] = "morning_volume_below_threshold"
                    observations.append(row)
                    if version == "A1":
                        state = "first_cross_rejected"
                    continue
                if version == "B1":
                    scale = meta.get((symbol, day), {}).get("price_scale")
                    if scale is None or not isfinite(scale) or scale <= 0:
                        row.update(
                            status="unknown",
                            reason="intraday_price_basis_unknown",
                            shape_match=None,
                            strict_signal=None,
                        )
                        observations.append(row)
                        state = "first_signal_unknown"
                        break
                    price = observed.raw_price * scale
                    row.update(raw_price=observed.raw_price, reference_price=price)
                    try:
                        temporary = temporary_daily_indicators(prior, price, params=params)
                    except ValueError:
                        row.update(
                            status="unknown",
                            reason="indicator_history_insufficient",
                            shape_match=None,
                            strict_signal=None,
                        )
                        observations.append(row)
                        state = "first_signal_unknown"
                        break
                    row.update({k: v for k, v in temporary.items() if k in row})
                    if not (prior["q"][-1] < row["q"] < 0):
                        row["reason"] = "intraday_gap_not_negative_and_converging"
                    elif not _at_least(intraday_params.gap_threshold, row["gap_distance"]):
                        row["reason"] = "gap_distance_exceeded"
                    elif not _at_least(params.zero_threshold, row["zero_distance"]):
                        row["reason"] = "zero_distance_exceeded"
                    elif (
                        row["ma60_lag"] is None
                        or price <= row["ma60"]
                        or row["ma60"] <= row["ma60_lag"]
                    ):
                        row["reason"] = "intraday_trend_rejected"
                if not row["reason"]:
                    _match(row)
                    state, signaled_at = "signal_consumed", row["signal_time"]
                observations.append(row)
                if signaled_at is not None:
                    break
                if version == "A1":
                    state = "first_cross_rejected"
            if state == "waiting_first_cross":
                if r_date is not None:
                    state = "first_cross_rejected"
                elif cycle["status"] == "cycle_unknown_gap":
                    state = "cycle_unknown_gap"
            if (r_date or cycle["audit_date"] or end) >= start:
                cycle_rows.append(
                    {
                        "version": version,
                        "symbol": symbol,
                        "g_date": cycle["g_date"],
                        "d_date": cycle["d_date"],
                        "r_date": r_date,
                        "status": state,
                        "signal_time": signaled_at,
                    }
                )
    output = pl.DataFrame(observations, schema=OBSERVATION_SCHEMA).filter(
        pl.col("signal_time").dt.date() >= start
    )
    return IntradayAuditResult(
        output,
        output.filter(pl.col("shape_match")),
        pl.DataFrame(cycle_rows, schema=INTRADAY_CYCLE_SCHEMA),
    )
