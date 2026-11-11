from datetime import date, timedelta

import polars as pl
import pytest

from app.services.index_const import CORE_INDEX_SYMBOLS
from app.services.market_game_calibration import (
    calibrate_history,
    index_metrics,
    prepare_history,
    run_local_calibration,
    winner_premium,
)


def history(n=30):
    return pl.DataFrame([{
        'date': date(2026, 1, 1) + timedelta(days=i), 'up_count': 60, 'down_count': 40,
        'total_amount': 1000., 'avg_turnover': 10., 'above_ma20_pct': .6,
        'median_pct': .01, 'limit_up': 20, 'limit_down': 2, 'seal_rate': .8,
        'max_consecutive': 7, 'promo_rate': .3,
    } for i in range(n)])


def test_units_missing_fields_and_past_only_windows():
    original = history()
    first = prepare_history(original.head(25))
    extended = prepare_history(original)
    assert first == extended[:25]
    assert first[-1]['breadth_up'] == .6
    assert first[-1]['median_return'] == .01
    assert first[-1]['amount_ratio'] == 1
    assert first[-1]['previous_limit_premium'] is None
    assert first[-1]['index_ret5'] is None
    assert first[-1]['max_boards'] == 7
    assert first[-1]['promotion_rate'] == .3


def test_partial_daily_supplement_cannot_replace_market_breadth():
    frame = history(2)
    rows = prepare_history(frame, premiums={frame['date'][1]: {
        'return_count': 1, 'up_count': 1, 'previous_limit_premium': .05,
    }})
    assert rows[1]['breadth_up'] == .6
    assert rows[1]['previous_limit_premium'] == .05


def test_incomplete_ladder_or_previous_fields_do_not_claim_complete_inputs():
    frame = history(3)
    indices = {day: {'index_return': .01, 'index_ret5': .01, 'index_ret20': .01,
                     'index_above_ma20': .75} for day in frame['date']}
    premiums = {day: {'previous_limit_premium': .02} for day in frame['date']}
    result = calibrate_history(frame, premiums=premiums, indices=indices)
    assert all(not row['complete_inputs'] for row in result['observations'])


def test_forward_outcomes_exclude_signal_day_and_censor_tail():
    result = calibrate_history(history(5))
    baseline = result['groups'][0]
    assert baseline['days'] == 5
    assert baseline['forward_1']['n'] == 4
    assert baseline['forward_1']['mean'] == pytest.approx(.01)
    assert baseline['forward_3']['n'] == 2
    assert baseline['forward_3']['mean'] == pytest.approx(1.01 ** 3 - 1)
    assert result['scope'] == 'in_sample_market_proxy'
    assert result['coverage']['previous_limit_premium'] == 0


def test_date_range_limits_outcomes_and_does_not_discard_warmup():
    frame = history()
    end = frame['date'][24]
    report = calibrate_history(frame, start=frame['date'][20], end=end)
    assert report['groups'][0]['days'] == 5
    assert report['groups'][0]['forward_3']['n'] == 2
    assert report['coverage']['amount_ratio'] == 5
    assert all(row['date'] <= end.isoformat() for row in report['observations'])


def test_missing_forward_day_not_zero_or_skipped():
    frame = history(5).with_columns(
        pl.when(pl.col('date') == date(2026, 1, 3)).then(None)
        .otherwise(pl.col('median_pct')).alias('median_pct')
    )
    result = calibrate_history(frame)
    assert result['groups'][0]['forward_3']['n'] == 0


def test_winner_premium_requires_adjacent_sessions_and_current_return():
    days = history(3)['date'].to_list()
    daily = pl.DataFrame({
        'date': [days[0], days[1], days[0], days[2]],
        'symbol': ['A', 'A', 'B', 'B'], 'close': [10., 11., 10., 15.],
        'consecutive_limit_ups': [1, 0, 1, 0],
    })
    result = winner_premium(daily, days)
    assert result[days[1]]['previous_limit_premium'] == pytest.approx(.1)
    assert result[days[2]]['previous_limit_premium'] is None


def test_empty_duplicate_invalid_dates():
    assert calibrate_history(pl.DataFrame())['status'] == 'unavailable'
    with pytest.raises(ValueError, match='重复'):
        calibrate_history(pl.concat([history(2), history(2)]))
    with pytest.raises(ValueError):
        calibrate_history(history(), start=date(2026, 2, 1), end=date(2026, 1, 1))


def test_indices_require_all_core_indices_and_contiguous_windows():
    days = history()['date'].to_list()
    prices = pl.DataFrame([{'date': day, 'symbol': symbol, 'close': 100. * 1.01 ** i}
                          for symbol in CORE_INDEX_SYMBOLS for i, day in enumerate(days)])
    full = index_metrics(prices, days)
    assert full[days[-1]]['index_ret5'] == pytest.approx(1.01 ** 5 - 1)
    assert full[days[10]]['index_ret20'] is None
    gap = prices.filter(~((pl.col('date') == days[-3])
                         & (pl.col('symbol') == CORE_INDEX_SYMBOLS[0])))
    assert index_metrics(gap, days)[days[-1]]['index_ret5'] is None
    assert index_metrics(prices.head(0), days) == {}


def test_offline_readers_accept_old_schema_and_preserve_sources(tmp_path):
    root = tmp_path / 'regime_history'
    root.mkdir()
    history().write_parquet(root / 'part.parquet')
    before = (root / 'part.parquet').read_bytes()
    report = run_local_calibration(tmp_path, start=date(2020, 1, 1))
    assert report['status'] == 'ready'
    assert report['coverage']['previous_limit_premium'] == 0
    assert (root / 'part.parquet').read_bytes() == before


def test_new_outcomes_quantiles_gate_groups_and_split():
    frame = history(80)
    days = frame['date'].to_list()
    premiums = {day: {'previous_limit_premium': .02, 'ew_return': .001 * (i % 3)} for i, day in enumerate(days)}
    entry = lambda level: {  # noqa: E731
        'sentiment': {'up_count': 60, 'down_count': 40, 'lt5_count': level, 'mian_count': level,
                      'damian_count': 0, 'limit_up': 20, 'limit_down': 2},
        'effect': {'lose_effect': level, 'earn_effect': 50},
    }
    sentiment = {day.isoformat(): entry(i % 10) for i, day in enumerate(days[:-1])}
    sentiment[days[-1].isoformat()] = entry(30)
    result = calibrate_history(frame, premiums=premiums, sentiment=sentiment, split=days[40])
    groups = {row['group']: row for row in result['groups']}
    # After the 60-session warmup, levels 8/9 rank at ~85/95 and the spike at 100.
    assert groups['gate:veto']['days'] == 4 and groups['gate:veto@test']['days'] == 4
    assert groups['gate:unavailable']['days'] == 60  # fewer than 60 earlier sessions
    assert groups['baseline@train']['days'] == 40 and groups['baseline@test']['days'] == 40
    baseline = groups['baseline']
    assert baseline['premium_1']['n'] == 79 and baseline['premium_1']['mean'] == pytest.approx(.02)
    assert baseline['ew_1']['n'] == 79 and baseline['ew_3']['n'] == 77
    assert baseline['forward_1']['p10'] == pytest.approx(.01)
    assert result['observations'][-1]['gate'] == 'veto'
    assert result['split'] == days[40].isoformat() and result['sentiment_days'] == 80
    plain = calibrate_history(frame)
    assert not any(row['group'].startswith('gate:') for row in plain['groups'])
    assert plain['observations'][0]['gate'] is None


def test_winner_premium_reports_equal_weight_return_without_extreme_rows():
    days = history(2)['date'].to_list()
    daily = pl.DataFrame({
        'date': [days[0], days[1], days[0], days[1], days[0], days[1]],
        'symbol': ['A', 'A', 'B', 'B', 'C', 'C'], 'close': [10., 11., 10., 9., 10., 20.],
        'consecutive_limit_ups': [0, 0, 0, 0, 0, 0],
    })
    assert winner_premium(daily, days)[days[1]]['ew_return'] == pytest.approx(0)


def _uptrend(frame, **extra):
    return {day: {'index_ret20': .02, 'index_return': .001, **extra} for day in frame['date']}


def _shadow(result):
    return [row['shadow'] for row in result['observations']]


def test_volume_shadow_splits_heat_days_by_price_response():
    frame = history(10).with_columns(
        pl.when(pl.col('date').is_in([date(2026, 1, 8), date(2026, 1, 9)])).then(1500.)
        .otherwise(pl.col('total_amount')).alias('total_amount'),
        pl.when(pl.col('date') == date(2026, 1, 8)).then(-.002)
        .otherwise(pl.col('median_pct')).alias('median_pct'),
    )
    shadow = _shadow(calibrate_history(frame, indices=_uptrend(frame)))
    assert shadow[7] == ['shadow:volume_stall']
    assert shadow[8] == ['shadow:volume_advance']  # ratio 1500 / 1100
    assert shadow[9] == []  # back below the heat threshold
    # Without an index trend the day joins neither side.
    assert all(keys == [] for keys in _shadow(calibrate_history(frame)))


@pytest.mark.parametrize(('up', 'down', 'expected'), [
    (.005, -.01, 'shadow:ease_down'), (.01, -.005, 'shadow:ease_up'), (.01, -.01, None),
])
def test_ease_shadow_compares_move_per_unit_amount(up, down, expected):
    frame = history(25).with_row_index('i').with_columns(
        pl.when(pl.col('i') % 2 == 0).then(up).otherwise(down).alias('median_pct'),
    ).drop('i')
    shadow = _shadow(calibrate_history(frame, indices=_uptrend(frame)))
    assert all(not keys for keys in shadow[:19])  # window needs 20 sessions
    assert all(keys == ([expected] if expected else []) for keys in shadow[19:])
    head = calibrate_history(frame.head(21), indices=_uptrend(frame))
    assert _shadow(head) == shadow[:21]  # later rows never change earlier groups


@pytest.mark.parametrize(('highs', 'expected'), [
    (2, ['shadow:first_drop_after_high']), (0, ['shadow:drop_no_high']), (1, []), (None, []),
])
def test_first_drop_shadow_requires_recent_highs_and_no_prior_drop(highs, expected):
    frame = history(14)
    days = frame['date'].to_list()
    indices = {day: {'index_return': .001, 'index_recent_highs': highs} for day in days}
    indices[days[10]] = {**indices[days[10]], 'index_return': -.02}
    indices[days[11]] = {**indices[days[11]], 'index_return': -.02}
    shadow = _shadow(calibrate_history(frame, indices=indices))
    assert shadow[10] == expected
    assert shadow[11] == []  # a second drop is no longer the first one
    indices[days[8]] = {**indices[days[8]], 'index_return': None}
    assert _shadow(calibrate_history(frame, indices=indices))[10] == []


def test_index_recent_highs_need_full_lookback_and_all_indices():
    days = history(70)['date'].to_list()
    prices = pl.DataFrame([{'date': day, 'symbol': symbol, 'close': 100. * 1.01 ** i}
                          for symbol in CORE_INDEX_SYMBOLS for i, day in enumerate(days)])
    full = index_metrics(prices, days)
    assert full[days[-1]]['index_recent_highs'] == 4
    assert full[days[62]]['index_recent_highs'] is None  # 60-session high plus 5 prior sessions
    falling = prices.with_columns(
        pl.when(pl.col('date') >= days[-6]).then(pl.col('close') * .5).otherwise(pl.col('close')).alias('close'))
    assert index_metrics(falling, days)[days[-1]]['index_recent_highs'] == 0
    partial = prices.filter(pl.col('symbol') != CORE_INDEX_SYMBOLS[0])
    assert index_metrics(partial, days)[days[-1]]['index_recent_highs'] is None


def test_shadow_groups_split_and_publish_fixed_definitions():
    frame = history(10).with_columns(
        pl.when(pl.col('date') == date(2026, 1, 8)).then(1500.).otherwise(pl.col('total_amount')).alias('total_amount'),
        pl.when(pl.col('date') == date(2026, 1, 8)).then(-.002).otherwise(pl.col('median_pct')).alias('median_pct'),
    )
    result = calibrate_history(frame, indices=_uptrend(frame), split=date(2026, 1, 5))
    groups = {row['group']: row['days'] for row in result['groups']}
    assert groups['shadow:volume_stall'] == groups['shadow:volume_stall@test'] == 1
    assert 'shadow:volume_stall@train' not in groups
    assert {item['group'] for item in result['shadow_definitions']} == {
        'shadow:volume_stall', 'shadow:ease_down', 'shadow:first_drop_after_high'}
