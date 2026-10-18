from datetime import date, timedelta

import polars as pl
import pytest

from app.indicators.pipeline import _compute_limit_signals_today
from app.strategy.first_board import (
    FirstBoardRules,
    build_candidates,
    evaluate_candidates,
    first_board_limit_price,
)

DAY = date(2026, 1, 20)


def candidates(symbol="600001.SH"):
    return pl.DataFrame([{
        "symbol": symbol, "name": "测试股票", "pattern": "platform", "pattern_label": "平台突破",
        "reference_date": DAY - timedelta(days=1), "reference_price": 10.0,
        "reference_adjusted_close": 5.0, "breakout_price": 5.1,
    }])


def quote(**updates):
    values = {
        "symbol": "600001.SH", "date": DAY, "name": "测试股票", "raw_close": 10.8,
        "close": 5.4, "raw_high": 10.8, "prev_close": 5.0,
        "limit_up_price": 10.995, "turnover_rate": 5.0, "amount": 80_000_000.0,
    }
    values.update(updates)
    return values


def evaluate(values, **kwargs):
    return evaluate_candidates(
        candidates(values["symbol"]), pl.DataFrame([values]), FirstBoardRules(), as_of=DAY, **kwargs,
    )[0]


def test_live_limit_uses_authoritative_threshold_and_adjusted_reference():
    row = evaluate(quote(limit_up_price=11.195, raw_close=11.0, close=5.5, raw_high=11.0))
    assert row["state"] == "approaching"
    assert row["evidence"]["limit_up_price"] == pytest.approx(11.2)
    assert row["evidence"]["raw_prev_close"] == pytest.approx(10.0)
    assert row["change_pct"] == pytest.approx(.1)
    assert row["distance_to_limit_pct"] == pytest.approx(.2 / 11.2)


@pytest.mark.parametrize(("price", "expected"), [(10.99, "approaching"), (11.0, "sealed")])
def test_live_threshold_tolerance_is_applied_once(price, expected):
    row = evaluate(quote(raw_close=price, close=price / 2, raw_high=price))
    assert row["state"] == expected


@pytest.mark.parametrize("limit", [None, float("nan"), float("inf"), 0.0, -1.0, True])
def test_live_invalid_or_no_limit_threshold_never_falls_back(limit):
    assert evaluate(quote(limit_up_price=limit, limit_up=11.0))["state"] == "invalid"


def test_live_missing_threshold_fails_closed_even_with_legacy_limit():
    values = quote(limit_up=11.0)
    del values["limit_up_price"]
    assert evaluate(values)["state"] == "invalid"


@pytest.mark.parametrize(("effective", "expected"), [(11.0, "approaching"), (10000.0, "invalid")])
def test_real_pipeline_output_preserves_threshold_and_unlimited_day(effective, expected):
    values = quote(raw_close=10.99, close=5.495, raw_high=10.99, _prev_close_raw=10.0)
    del values["limit_up_price"]
    instruments = pl.DataFrame([{
        "symbol": "600001.SH", "name": "测试股票", "limit_up": effective,
        "limit_down": 9.0, "as_of": DAY, "listing_date": date(2000, 1, 1),
    }])
    current = _compute_limit_signals_today(pl.DataFrame([values]), instruments)
    assert "limit_up" not in current.columns
    row = evaluate_candidates(candidates(), current, FirstBoardRules(), as_of=DAY)[0]
    assert row["state"] == expected


@pytest.mark.parametrize("symbol", ["600001.SH", "000001.SZ", "300001.SZ", "688001.SH", "689001.SH"])
def test_supported_boards_use_current_effective_limit(symbol):
    limit = 12.0 if symbol.startswith(("300", "688", "689")) else 11.0
    price = limit - .1
    row = evaluate(quote(symbol=symbol, raw_close=price, close=price / 2, raw_high=price,
                         limit_up_price=limit - .005))
    assert row["state"] == "approaching"
    assert row["evidence"]["limit_up_price"] == pytest.approx(limit)


@pytest.mark.parametrize("updates", [
    {"date": DAY - timedelta(days=1)}, {"symbol": "830001.BJ"}, {"name": "*ST测试"},
    {"raw_close": None}, {"close": None}, {"raw_high": 10.0}, {"raw_high": 11.5},
])
def test_invalid_current_quotes_do_not_produce_candidates(updates):
    assert evaluate(quote(**updates))["state"] == "invalid"


def test_missing_high_and_decimal_turnover_do_not_pass():
    assert evaluate(quote(raw_high=None))["state"] == "watch"
    assert evaluate(quote(turnover_rate=.05))["state"] == "watch"
    assert evaluate(quote())["state"] == "approaching"


def test_duplicate_quote_fails_closed_and_break_is_observed():
    current = pl.DataFrame([quote(), quote()])
    row = evaluate_candidates(candidates(), current, FirstBoardRules(), as_of=DAY)[0]
    assert row["state"] == "invalid"
    assert evaluate(quote(raw_high=11.0))["state"] == "broken"


@pytest.mark.parametrize(("day", "expected"), [
    (date(2020, 8, 21), 11.0), (date(2020, 8, 24), 12.0),
])
def test_historical_chinext_preserves_date_rule(day, expected):
    assert first_board_limit_price("300001.SZ", day, 10.0) == expected
    if day < date(2020, 8, 24):
        assert first_board_limit_price("300001.SZ", day, 10.0, explicit=12.0) == 11.0


def history(count=25):
    days = []
    day = date(2025, 9, 1)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return pl.DataFrame([{
        "symbol": symbol, "name": "测试股票", "date": value,
        "open": 5.0, "high": 5.05, "low": 4.95, "close": 5.0,
        "raw_close": 10.0,
    } for symbol in ("600001.SH", "000001.SZ") for value in days])


def short_rules():
    return FirstBoardRules(min_history_days=20, enabled_patterns=["platform"])


def test_candidates_only_use_completed_days_and_adjusted_shapes():
    frame = history()
    as_of = frame["date"].max() + timedelta(days=1)
    expected = build_candidates(frame, as_of, short_rules())
    future = frame.tail(1).with_columns(
        pl.lit(as_of).alias("date"), pl.lit(50.0).alias("close"),
    )
    actual = build_candidates(pl.concat([frame, future]), as_of, short_rules())
    assert expected.height == 2
    assert actual.equals(expected)
    assert expected["breakout_price"].to_list() == [5.05, 5.05]


def test_history_gap_duplicate_and_missing_column_are_rejected():
    frame = history()
    as_of = frame["date"].max() + timedelta(days=1)
    missing_day = frame["date"][12]
    gap = frame.filter(~((pl.col("symbol") == "600001.SH") & (pl.col("date") == missing_day)))
    assert build_candidates(gap, as_of, short_rules())["symbol"].to_list() == ["000001.SZ"]
    assert build_candidates(frame.drop("raw_close"), as_of, short_rules()).is_empty()
    with pytest.raises(ValueError, match="重复"):
        build_candidates(pl.concat([frame, frame.head(1)]), as_of, short_rules())


def test_history_limit_in_lookback_excludes_stock():
    frame = history()
    day = frame["date"].max()
    target = (pl.col("symbol") == "600001.SH") & (pl.col("date") == day)
    frame = frame.with_columns(
        pl.when(target).then(5.5).otherwise(pl.col("close")).alias("close"),
        pl.when(target).then(5.5).otherwise(pl.col("high")).alias("high"),
        pl.when(target).then(11.0).otherwise(pl.col("raw_close")).alias("raw_close"),
    )
    assert build_candidates(frame, day + timedelta(days=1), short_rules())["symbol"].to_list() == ["000001.SZ"]


def test_history_chinext_pre_reform_corrects_runtime_twenty_percent_cache():
    frame = history().filter(pl.col("symbol") == "600001.SH")
    offset = frame["date"].max() - date(2020, 8, 21)
    frame = frame.with_columns(
        pl.lit("300001.SZ").alias("symbol"),
        (pl.col("date") - offset).alias("date"),
        pl.lit(11.995).alias("limit_up_price"),
    )
    target = pl.col("date") == date(2020, 8, 21)
    frame = frame.with_columns(
        pl.when(target).then(5.5).otherwise(pl.col("close")).alias("close"),
        pl.when(target).then(5.5).otherwise(pl.col("high")).alias("high"),
        pl.when(target).then(11.0).otherwise(pl.col("raw_close")).alias("raw_close"),
    )
    assert build_candidates(frame, date(2020, 8, 24), short_rules()).is_empty()


def test_ex_rights_history_uses_adjusted_previous_close_on_current_raw_scale():
    frame = history()
    last_day = frame["date"].max()
    frame = frame.with_columns(
        pl.when(pl.col("date") == last_day).then(5.0).otherwise(pl.col("raw_close")).alias("raw_close"),
    )
    result = build_candidates(frame, last_day + timedelta(days=1), short_rules())
    assert result.height == 2
    assert result["first_board_count"].to_list() == [0, 0]
