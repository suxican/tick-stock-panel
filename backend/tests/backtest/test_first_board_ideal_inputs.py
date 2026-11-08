from datetime import date, datetime, time, timedelta

import polars as pl
import pytest

from app.backtest.first_board_ideal_inputs import prepare_first_board_ideal_candidates
from app.market_time import CN_TZ


def _days(count):
    days = []
    day = date(2025, 1, 2)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _inputs(count=100):
    days = _days(count)
    raw = pl.DataFrame({
        "symbol": ["600001.SH"] * count, "date": days,
        "open": [10.] * count, "high": [10.2] * count,
        "low": [9.8] * count, "close": [10.] * count,
        "volume": [100_000.] * count, "amount": [1_000_000.] * count,
    })
    instruments = pl.DataFrame({
        "symbol": ["600001.SH"], "name": ["样本"], "listing_date": [date(2000, 1, 1)],
    })
    return raw, pl.DataFrame(), instruments, days


def _prepare(raw, factors, instruments, days, *, index=70, pattern="platform", params=None):
    if params is None:
        params = {"min_history_days": 20, "lookback_days": 10}
    return prepare_first_board_ideal_candidates(
        raw, factors, instruments, days, pattern=pattern, params=params,
        start=days[index], end=days[index],
    )


def _factor(day, value=1.02):
    return pl.DataFrame({"symbol": ["600001.SH"], "trade_date": [day], "ex_factor": [value]})


def test_shape_is_prior_session_and_limit_is_exact_raw_price():
    raw, factors, instruments, days = _inputs()
    # T changes cannot create its own historical shape or alter yesterday's limit base.
    raw = raw.with_columns(pl.when(pl.col("date") == days[70]).then(11.)
                           .otherwise(pl.col("high")).alias("high"))
    rows, audit = _prepare(raw, factors, instruments, days)
    row = rows.row(0, named=True)
    assert rows.height == 1
    assert row["date"] == days[70]
    assert row["reference_date"] == days[69]
    assert row["exit_date"] == days[71]
    assert row["window_start"] == days[49]
    assert row["limit_up_price"] == 11.
    assert row["exclude_reason"] is None
    assert audit["current_metadata_only"] is True
    assert audit["historical_st_verified"] is False
    assert audit["eligible_candidates"] == 1


def test_current_day_shape_is_not_used_for_same_day_entry():
    raw, factors, instruments, days = _inputs()
    raw = raw.with_columns(pl.when(pl.col("date") == days[69]).then(20.)
                           .otherwise(pl.col("high")).alias("high"))
    rows, _ = _prepare(raw, factors, instruments, days)
    assert rows.is_empty()


def test_trend_prior_session_signal_uses_saved_thresholds_and_exact_same_day_limit():
    raw, factors, instruments, days = _inputs()
    raw = raw.with_columns(pl.Series("close", [10 + i * .02 for i in range(raw.height)]))
    raw = raw.with_columns(pl.col("close").alias("open"),
                           (pl.col("close") + .1).alias("high"),
                           (pl.col("close") - .1).alias("low"))
    raw = raw.with_columns(pl.when(pl.col("date") == days[70]).then(12.52)
                           .otherwise(pl.col("high")).alias("high"))
    params = {"min_history_days": 60, "lookback_days": 35, "trend_window": 40,
              "trend_min_return": .03, "trend_max_return": .4}
    rows, audit = _prepare(raw, factors, instruments, days, pattern="trend", params=params)
    assert rows.height == audit["eligible_candidates"] == 1
    assert rows.select("date", "reference_date", "exit_date").row(0) == (days[70], days[69], days[71])
    assert rows["limit_up_price"].to_list() == [12.52]
    assert rows["exclude_reason"].to_list() == [None]
    for changed in ({"trend_min_return": .2}, {"trend_max_return": .05}, {"trend_window": 10}):
        fewer, _ = _prepare(raw, factors, instruments, days, pattern="trend", params={**params, **changed})
        assert fewer.is_empty()


@pytest.mark.parametrize("event_index", [49, 60, 70, 71])
def test_any_action_in_reference_through_exit_window_is_excluded(event_index):
    raw, _, instruments, days = _inputs()
    rows, audit = _prepare(raw, _factor(days[event_index]), instruments, days)
    assert rows["exclude_reason"].to_list() == ["corporate_action_window"]
    assert audit["exclusions"] == {"corporate_action_window": 1}


@pytest.mark.parametrize("event_index", [48, 72])
def test_action_outside_window_does_not_exclude(event_index):
    raw, _, instruments, days = _inputs()
    rows, _ = _prepare(raw, _factor(days[event_index]), instruments, days)
    assert rows["exclude_reason"].to_list() == [None]


def test_nontrading_day_action_cannot_escape_date_interval_check():
    raw, _, instruments, days = _inputs()
    weekend = next(day + timedelta(days=1) for day in days[50:65] if day.weekday() == 4)
    rows, _ = _prepare(raw, _factor(weekend), instruments, days)
    assert rows["exclude_reason"].to_list()[0] in {"corporate_action_window", "factor_quarantine"}


def test_bad_factor_quarantine_does_not_expire_after_sixty_one_sessions():
    raw, _, instruments, days = _inputs(130)
    rows, audit = _prepare(raw, _factor(days[10], 501.3259), instruments, days, index=100)
    assert rows["window_start"][0] > days[10]
    assert rows["exclude_reason"].to_list() == ["factor_quarantine"]
    assert audit["factor_quarantine_from"] == {"600001.SH": str(days[10])}
    assert audit["factor_check_start"] == str(days[0])


@pytest.mark.parametrize("value", [None, 0., -1., float("nan"), float("inf")])
def test_malformed_factor_quarantines_all_later_candidates(value):
    raw, _, instruments, days = _inputs()
    rows, _ = _prepare(raw, _factor(days[10], value), instruments, days)
    assert rows["exclude_reason"].to_list() == ["factor_quarantine"]


def test_undated_factor_quarantines_entire_symbol():
    raw, _, instruments, days = _inputs()
    rows, audit = _prepare(raw, _factor(None), instruments, days)
    assert rows["exclude_reason"].to_list() == ["factor_quarantine"]
    assert audit["factor_quarantine_from"] == {"600001.SH": None}


def test_unit_factor_has_no_effect():
    raw, _, instruments, days = _inputs()
    rows, _ = _prepare(raw, _factor(days[60], 1.), instruments, days)
    assert rows["exclude_reason"].to_list() == [None]


def test_missing_market_session_is_not_compressed_by_dense_rank():
    raw, factors, instruments, days = _inputs()
    raw = raw.filter(pl.col("date") != days[60])
    rows, audit = _prepare(raw, factors, instruments, days)
    assert rows["exclude_reason"].to_list() == ["history_gap"]
    assert str(days[60]) in audit["missing_market_days"]


@pytest.mark.parametrize(("index", "reason"), [(70, "missing_entry_day"), (71, "missing_exit_day")])
def test_missing_entry_and_exit_rows_are_explicit(index, reason):
    raw, factors, instruments, days = _inputs()
    raw = raw.filter(pl.col("date") != days[index])
    rows, _ = _prepare(raw, factors, instruments, days)
    assert rows["exclude_reason"].to_list() == [reason]


@pytest.mark.parametrize(("index", "column", "value"), [
    (70, "high", 11.02), (71, "high", 11.02), (70, "low", 8.98), (71, "low", 8.98),
])
def test_execution_prices_outside_daily_limits_are_excluded(index, column, value):
    raw, factors, instruments, days = _inputs()
    raw = raw.with_columns(pl.when(pl.col("date") == days[index]).then(value)
                           .otherwise(pl.col(column)).alias(column))
    rows, audit = _prepare(raw, factors, instruments, days)
    assert rows["exclude_reason"].to_list() == ["daily_price_outside_limits"]
    assert audit["exclusions"] == {"daily_price_outside_limits": 1}


def test_one_tick_limit_tolerance_is_explicit():
    raw, factors, instruments, days = _inputs()
    raw = raw.with_columns(pl.when(pl.col("date") == days[71]).then(11.01)
                           .otherwise(pl.col("high")).alias("high"))
    rows, _ = _prepare(raw, factors, instruments, days)
    assert rows["exclude_reason"].to_list() == [None]


@pytest.mark.parametrize("value", [0., -1., None])
def test_halted_or_missing_volume_cannot_supply_history(value):
    raw, factors, instruments, days = _inputs()
    raw = raw.with_columns(pl.when(pl.col("date") == days[60]).then(value)
                           .otherwise(pl.col("volume")).alias("volume"))
    rows, audit = _prepare(raw, factors, instruments, days)
    assert rows["exclude_reason"].to_list() == ["history_gap"]
    assert audit["invalid_price_rows"] == 1


@pytest.mark.parametrize(("stamp_day", "stamp_time", "expected"), [
    (70, time(14, 59, 59), "missing_entry_day"),
    (71, time(15), "missing_entry_day"),
    (70, time(15), None),
])
def test_snapshot_requires_its_own_beijing_date_after_close(stamp_day, stamp_time, expected):
    raw, factors, instruments, days = _inputs()
    stamp = int(datetime.combine(days[stamp_day], stamp_time, CN_TZ).timestamp() * 1000)
    raw = raw.with_columns(pl.when(pl.col("date") == days[70]).then(stamp)
                           .otherwise(None).alias("quote_ts"))
    rows, audit = _prepare(raw, factors, instruments, days)
    assert rows["exclude_reason"].to_list() == [expected]
    assert audit["incomplete_rows"] == int(expected is not None)


@pytest.mark.parametrize("column", ["name", "listing_date"])
def test_missing_metadata_cannot_enter_current_universe(column):
    raw, factors, instruments, days = _inputs()
    rows, audit = _prepare(raw, factors, instruments.drop(column), days)
    assert rows.is_empty()
    assert audit["missing_metadata_symbols"] == 1


@pytest.mark.parametrize("kind", ["raw", "factor", "instrument", "calendar"])
def test_duplicates_fail_closed(kind):
    raw, factors, instruments, days = _inputs()
    if kind == "raw":
        raw = pl.concat([raw, raw.head(1)])
    elif kind == "factor":
        factors = pl.concat([_factor(days[60]), _factor(days[60])])
    elif kind == "instrument":
        instruments = pl.concat([instruments, instruments])
    else:
        days = [*days, days[-1]]
    with pytest.raises(ValueError, match=r"duplicate|sorted unique"):
        _prepare(raw, factors, instruments, days)


def test_longest_history_window_includes_predecessor():
    raw, factors, instruments, days = _inputs(260)
    params = {"min_history_days": 250, "lookback_days": 60, "platform_window": 60}
    rows, audit = _prepare(raw, factors, instruments, days, index=251, params=params)
    assert rows["window_start"].to_list() == [days[0]]
    assert audit["required_history_bars"] == 251
    assert rows["exclude_reason"].to_list() == [None]


def test_oversold_shape_uses_shared_threshold_and_changes_with_parameters():
    raw, factors, instruments, days = _inputs()
    raw = raw.with_columns(pl.when(pl.col("date") < days[40]).then(14.)
                           .otherwise(10.).alias("close"))
    raw = raw.with_columns(pl.col("close").alias("open"),
                           (pl.col("close") + .2).alias("high"),
                           (pl.col("close") - .2).alias("low"))
    params = {"min_history_days": 60, "lookback_days": 35, "oversold_window": 60,
              "oversold_min_drawdown": .25}
    rows, _ = _prepare(raw, factors, instruments, days, pattern="oversold", params=params)
    assert rows["exclude_reason"].to_list() == [None]
    params["oversold_min_drawdown"] = .4
    rows, _ = _prepare(raw, factors, instruments, days, pattern="oversold", params=params)
    assert rows.is_empty()


@pytest.mark.parametrize("name", ["ST样本", "退市样本", "   "])
def test_current_universe_name_exclusions_are_applied(name):
    raw, factors, instruments, days = _inputs()
    instruments = instruments.with_columns(pl.lit(name).alias("name"))
    rows, _ = _prepare(raw, factors, instruments, days)
    assert rows.is_empty()


def test_recent_ipo_cannot_bypass_shared_no_limit_or_minimum_history():
    raw, factors, instruments, days = _inputs()
    instruments = instruments.with_columns(pl.lit(days[67]).alias("listing_date"))
    rows, _ = _prepare(raw, factors, instruments, days)
    assert rows.is_empty()


def test_future_prices_and_events_beyond_exit_tail_do_not_change_candidates():
    raw, factors, instruments, days = _inputs()
    expected, _ = _prepare(raw, factors, instruments, days)
    changed = raw.with_columns([
        pl.when(pl.col("date") > days[71]).then(pl.col(column) * 5)
        .otherwise(pl.col(column)).alias(column) for column in ("open", "high", "low", "close")
    ])
    actual, _ = _prepare(changed, _factor(days[72], 500.), instruments, days)
    assert actual.equals(expected)


def test_calendar_must_include_exit_tail_and_cannot_omit_market_dates():
    raw, factors, instruments, days = _inputs()
    with pytest.raises(ValueError, match="exit tail"):
        _prepare(raw.filter(pl.col("date") <= days[70]), factors, instruments, days[:71])
    with pytest.raises(ValueError, match="outside the independent"):
        _prepare(raw, factors, instruments, [day for day in days if day != days[60]])
