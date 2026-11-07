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
