import json
from datetime import date, datetime

import polars as pl
import pytest

from app.backtest.first_board_ideal import evaluate_first_board_ideal

FRIDAY, MONDAY, TUESDAY = date(2026, 10, 16), date(2026, 10, 19), date(2026, 10, 20)
SYMBOL = "600001.SH"


def candidates(**overrides):
    return pl.DataFrame([{"symbol": SYMBOL, "name": "测试股票", "date": FRIDAY,
                          "pattern": "platform", "limit_up_price": 10., **overrides}])


def bar(day, **overrides):
    values = {"symbol": SYMBOL, "date": day, "open": 10., "high": 10.8,
              "low": 9.6, "close": 10.2, "volume": 1000.}
    if day == FRIDAY:
        values.update(open=9.8, high=10., low=9.7, close=10.)
    return {**values, **overrides}


def run(*, signal=None, rows=None, days=None, **options):
    return evaluate_first_board_ideal(
        candidates() if signal is None else signal,
        pl.DataFrame([bar(FRIDAY), bar(MONDAY)]) if rows is None else pl.DataFrame(rows),
        [FRIDAY, MONDAY, TUESDAY] if days is None else days,
        **options,
    )


def test_nonstop_trade_uses_next_market_day_high_and_independent_net_costs():
    result = run()
    trade = result["trades"][0]
    assert trade["entry_date"] == FRIDAY.isoformat()
    assert trade["exit_date"] == MONDAY.isoformat()
    assert trade["entry_price"] == 10.
    assert trade["exit_price"] == 10.8
    assert trade["exit_reason"] == "next_day_high"
    assert trade["ideal"] and result["ideal"]
    assert result["parameters"]["slippage_bps"] == 0
    expected = 10.8 * (1 - .0003 - .0005) / (10 * (1 + .0003)) - 1
    assert trade["net_return"] == pytest.approx(expected)
    assert trade["buy_commission"] == pytest.approx(.003)
    assert trade["sell_commission"] == pytest.approx(.00324)
    assert trade["stamp_tax"] == pytest.approx(.0054)
    assert result["summary"]["mean_net_return"] == pytest.approx(expected)
    assert result["summary"]["win_rate"] == 1
    assert json.loads(json.dumps(result, allow_nan=False)) == result


@pytest.mark.parametrize(("opening", "expected"), [(9., 9.), (9.5, 9.5)])
def test_gap_stop_including_exact_five_percent_sells_open(opening, expected):
    result = run(rows=[bar(FRIDAY), bar(MONDAY, open=opening, low=8.8, high=11.)])
    trade = result["trades"][0]
    assert trade["stop_price"] == 9.5
    assert trade["exit_price"] == expected
    assert trade["exit_reason"] == "gap_stop"
    assert not trade["daily_sequence_ambiguous"]
    assert trade["next_high"] == 11.
    assert trade["high_upper_bound_net_return"] > trade["net_return"]


def test_intraday_stop_has_priority_even_if_daily_high_is_profitable():
    result = run(rows=[bar(FRIDAY), bar(MONDAY, open=10., low=9.5, high=11.)])
    trade = result["trades"][0]
    assert trade["exit_price"] == 9.5
    assert trade["exit_reason"] == "intraday_stop"
    assert trade["daily_sequence_ambiguous"]
    assert result["summary"]["ambiguous_count"] == 1
    assert trade["high_upper_bound_net_return"] > 0 > trade["net_return"]
    assert result["summary"]["mean_high_upper_bound_net_return"] == trade["high_upper_bound_net_return"]


def test_decimal_stop_equality_does_not_miss_trigger_due_to_binary_rounding():
    result = run(signal=candidates(limit_up_price=7.6), rows=[
        bar(FRIDAY, open=7.5, high=7.6, low=7.4, close=7.6),
        bar(MONDAY, open=7.4, high=7.7, low=7.22, close=7.5),
    ])
    assert result["trades"][0]["stop_price"] == 7.22
    assert result["trades"][0]["exit_reason"] == "intraday_stop"


def test_missing_next_day_is_excluded_without_substituting_later_bar():
    result = run(rows=[bar(FRIDAY), bar(TUESDAY, high=20.)])
    assert not result["trades"]
    assert result["summary"]["touched"] == 1
    excluded = result["exclusions"][0]
    assert excluded["required_exit_date"] == MONDAY.isoformat()
    assert excluded["reason_code"] == "nextday_missing"
    assert excluded["exit_price"] is None


def test_no_next_calendar_day_cannot_use_an_available_future_bar():
    result = run(days=[FRIDAY], rows=[bar(FRIDAY), bar(MONDAY)])
    assert result["exclusions"][0]["reason_code"] == "next_session_missing"
    assert not result["trades"]


def test_suspended_next_day_is_not_a_fabricated_mandatory_exit():
    result = run(rows=[bar(FRIDAY), bar(MONDAY, volume=0.), bar(TUESDAY, high=20.)])
    assert result["exclusions"][0]["reason_code"] == "exit_suspended"
    assert not result["trades"]


def test_one_price_entry_and_limit_down_like_exit_are_explicit_ideal_fills():
    result = run(rows=[bar(FRIDAY, open=10., high=10., low=10., close=10.),
                       bar(MONDAY, open=9., high=9., low=9., close=9.)])
    trade = result["trades"][0]
    assert trade["one_price_entry"] and trade["ideal"]
    assert trade["exit_price"] == 9.
    assert any("一字涨停" in text for text in result["limitations"])
    assert any("跌停" in text for text in result["limitations"])


@pytest.mark.parametrize("high", [9.995, 9.9999999])
def test_touch_uses_exact_limit_without_half_cent_runtime_tolerance(high):
    result = run(rows=[bar(FRIDAY, high=high, close=9.99)])
    assert result["exclusions"][0]["reason_code"] == "not_touched"
    assert result["summary"]["not_touched"] == 1
    assert result["summary"]["touched"] == 0
    assert result["summary"]["data_excluded"] == 0


def test_an_untriggered_signal_is_distinct_from_missing_entry_data():
    result = run(rows=[bar(MONDAY)])
    assert result["exclusions"][0]["reason_code"] == "entry_bar_missing"
    assert result["exclusions"][0]["touched"] is None
    assert result["summary"]["not_touched"] == 0
    assert result["summary"]["data_excluded"] == 1


def test_entry_limit_must_be_inside_daily_traded_range():
    result = run(rows=[bar(FRIDAY, open=11., high=11., low=11., close=11.), bar(MONDAY)])
    assert result["exclusions"][0]["reason_code"] == "entry_price_outside_bar"
    assert not result["trades"]


def test_unrepresentable_returns_are_excluded_and_json_remains_finite():
    result = run(signal=candidates(limit_up_price=.01), rows=[
        bar(FRIDAY, open=.01, high=.01, low=.01, close=.01),
        bar(MONDAY, open=1e308, high=1e308, low=1e308, close=1e308),
    ])
    assert result["exclusions"][0]["reason_code"] == "model_values_invalid"
    assert not result["trades"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("changes", [
    {"open": None}, {"close": float("nan")}, {"high": float("inf")},
    {"low": 0.}, {"low": 11., "high": 10.}, {"open": 11.},
])
def test_missing_or_invalid_next_prices_are_excluded(changes):
    result = run(rows=[bar(FRIDAY), bar(MONDAY, **changes)])
    assert result["exclusions"][0]["reason_code"] == "exit_prices_invalid"
    assert result["summary"]["trades"] == 0
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(("changes", "reason"), [
    ({"volume": 0.}, "entry_suspended"), ({"volume": None}, "entry_volume_invalid"),
    ({"volume": -1.}, "entry_volume_invalid"), ({"open": 11.}, "entry_prices_invalid"),
])
def test_entry_requires_valid_prices_and_positive_volume(changes, reason):
    result = run(rows=[bar(FRIDAY, **changes), bar(MONDAY)])
    assert result["exclusions"][0]["reason_code"] == reason
    assert result["summary"]["touched"] == 0


@pytest.mark.parametrize("price", [None, 0., -1., float("nan"), float("inf"), True, 10000., 10.995, 1.755])
def test_invalid_limit_price_is_excluded(price):
    result = run(signal=candidates(limit_up_price=price))
    assert result["exclusions"][0]["reason_code"] == "invalid_limit_price"
    json.dumps(result, allow_nan=False)


def test_low_price_touch_uses_exact_cents_not_runtime_threshold():
    result = run(signal=candidates(limit_up_price=1.76), rows=[
        bar(FRIDAY, open=1.70, high=1.755, low=1.69, close=1.75),
        bar(MONDAY, open=1.76, high=1.8, low=1.7, close=1.78),
    ])
    assert result["exclusions"][0]["reason_code"] == "not_touched"


def test_float_noise_in_exact_limit_or_touch_does_not_reject_valid_input():
    result = run(signal=candidates(limit_up_price=10.000000001), rows=[
        bar(FRIDAY, high=9.999999999, close=9.999999999), bar(MONDAY),
    ])
    assert result["summary"]["trades"] == 1


def test_day_high_over_limit_by_more_than_one_cent_is_inconsistent():
    result = run(rows=[bar(FRIDAY, high=10.02), bar(MONDAY)])
    assert result["exclusions"][0]["reason_code"] == "entry_high_above_limit"
    assert result["summary"]["touched"] == 0


def test_candidate_exclusion_precedes_price_checks_and_corporate_action_blocks_exit():
    result = run(signal=candidates(limit_up_price=None, exclude_reason="复权数据污染"))
    assert result["exclusions"][0]["reason_code"] == "source_excluded"
    assert result["exclusions"][0]["reason"] == "复权数据污染"
    result = run(rows=[bar(FRIDAY), bar(MONDAY, exclude_reason="除权跨日不可比")])
    assert result["summary"]["touched"] == 1
    assert result["exclusions"][0]["reason"] == "除权跨日不可比"
    assert not result["trades"]


def test_multi_pattern_stock_day_counts_once_but_retains_both_shapes():
    signal = pl.concat([candidates(), candidates(pattern="oversold")])
    result = run(signal=signal)
    assert result["summary"]["signal_rows"] == 2
    assert result["summary"]["signals"] == result["summary"]["trades"] == 1
    assert result["trades"][0]["patterns"] == ["oversold", "platform"]


def test_duplicate_signal_daily_bar_and_conflicting_stock_day_price_are_rejected():
    with pytest.raises(ValueError, match="重复"):
        run(signal=pl.concat([candidates(), candidates()]))
    with pytest.raises(ValueError, match="重复"):
        run(rows=[bar(FRIDAY), bar(MONDAY), bar(MONDAY)])
    with pytest.raises(ValueError, match="不一致"):
        run(signal=pl.concat([candidates(), candidates(pattern="oversold", limit_up_price=11.)]))


def test_later_prices_cannot_change_next_session_exit_or_summary():
    base = run()
    future = run(rows=[bar(FRIDAY), bar(MONDAY), bar(TUESDAY, open=20., high=99., low=1., close=30.)])
    assert future == base


def test_two_samples_use_arithmetic_mean_not_compounded_return():
    signal = pl.concat([candidates(), candidates(symbol="600002.SH")])
    rows = [bar(FRIDAY), bar(MONDAY), bar(FRIDAY, symbol="600002.SH"),
            bar(MONDAY, symbol="600002.SH", open=9., high=9., low=9., close=9.)]
    result = run(signal=signal, rows=rows, commission_pct=0, stamp_tax_pct=0)
    assert result["summary"]["mean_net_return"] == pytest.approx((.08 - .1) / 2)
    assert result["summary"]["median_net_return"] == pytest.approx(-.01)
    assert result["summary"]["win_rate"] == .5
    assert "cumulative_return" not in result["summary"]


@pytest.mark.parametrize("days", [[MONDAY, FRIDAY], [FRIDAY, FRIDAY], [datetime(2026, 10, 16)]])
def test_ambiguous_calendar_order_or_types_are_rejected(days):
    with pytest.raises(ValueError, match="日历"):
        run(days=days)


def test_signal_outside_calendar_and_empty_inputs_are_explicit():
    result = run(days=[MONDAY, TUESDAY])
    assert result["exclusions"][0]["reason_code"] == "signal_not_in_calendar"
    result = evaluate_first_board_ideal(pl.DataFrame(), pl.DataFrame(), [])
    assert result["summary"]["signals"] == 0
    assert result["summary"]["mean_net_return"] is None
    assert result["summary"]["win_rate"] is None
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("options", [
    {"stop_loss_pct": 0}, {"stop_loss_pct": 1}, {"stop_loss_pct": True},
    {"commission_pct": -.1}, {"stamp_tax_pct": float("nan")},
    {"commission_pct": .5, "stamp_tax_pct": .5},
])
def test_invalid_model_parameters_are_rejected(options):
    with pytest.raises(ValueError):
        run(**options)
