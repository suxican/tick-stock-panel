"""资金状态轴(market_capital)单元测试 — 成交集中度聚合与全序列标注。"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from app.services.market_capital import (
    CONC_CONCENTRATED,
    CONC_DISPERSED,
    CONC_NEUTRAL,
    DERIVED_COLUMNS,
    FLAG_SHRINK_ADVANCE,
    FLAG_VOLUME_STALL,
    VOLUME_EXPAND,
    VOLUME_FLAT,
    VOLUME_SHRINK,
    capital_daily_aggs,
    classify_capital_series,
    finalize_capital_row,
    with_amount_rank,
)


def _series(amounts, *, counts=None, medians=None, shares=None):
    """从 2024-01-01 起每个工作日一行。"""
    n = len(amounts)
    days, cur = [], date(2024, 1, 1)
    while len(days) < n:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return pl.DataFrame({
        "date": days, "total_amount": [float(a) for a in amounts],
        "amount_valid_count": counts or [5000] * n,
        "median_pct": medians or [0.001] * n,
        "amount_top5_share": shares or [0.40] * n,
    })


def _last(df, col):
    return df[col][-1]


def _agg_row(amounts):
    df = pl.DataFrame({"date": [date(2024, 1, 2)] * len(amounts), "amount": amounts})
    return with_amount_rank(df).group_by("date").agg(*capital_daily_aggs()).to_dicts()[0]


# ───────────────────────── 日聚合: 前 5% 个股成交占比 ─────────────────────────

def test_top5_share_from_daily_aggs():
    # 200 只股票成交额 1..200, 另有 1 只零成交、1 只空值
    out = finalize_capital_row(_agg_row([float(i) for i in range(1, 201)] + [0.0, None]))
    assert out["amount_valid_count"] == 200
    assert out["amount_top5_share"] == round(1955 / 20100, 6)  # 前 10 只: 191..200


def test_top5_count_rounds_up():
    # 101 只 → 前 ceil(5.05)=6 只
    out = finalize_capital_row(_agg_row([float(i) for i in range(1, 102)]))
    assert out["amount_top5_share"] == round(sum(range(96, 102)) / sum(range(1, 102)), 6)


def test_top5_share_null_below_100_stocks():
    assert finalize_capital_row(_agg_row([1.0] * 99)) == {
        "amount_valid_count": 99, "amount_top5_share": None}


def test_finalize_without_aggregates_is_empty_not_zero_share():
    assert finalize_capital_row({}) == {"amount_valid_count": 0, "amount_top5_share": None}


# ───────────────────────── 序列标注 ─────────────────────────

def test_multiple_uses_median_of_prior_20_days():
    out = classify_capital_series(_series([100.0] * 20 + [150.0]))
    assert _last(out, "amount_multiple") == 1.5
    assert out["amount_multiple"][14] is None  # 此前只有 14 个可比日
    assert out["amount_multiple"][15] == 1.0   # 满 15 个即可计算


def test_incomplete_day_is_not_comparable():
    # 第 21 天有效股票数骤降 20%: 当日无可比基准; 之后的日子把它排除
    counts = [5000] * 20 + [4000] + [5000] * 5
    out = classify_capital_series(_series([100.0] * 20 + [60.0] + [100.0] * 5, counts=counts))
    assert out["amount_multiple"][20] is None
    assert out["capital_volume"][20] is None
    assert out["amount_multiple"][25] == 1.0  # 20 行里 19 个可比, 中位数仍为 100


def test_multiple_null_when_fewer_than_15_comparable():
    counts = [4000] * 6 + [5000] * 15  # 最后一天前 20 行只有 14 个可比
    out = classify_capital_series(_series([100.0] * 21, counts=counts))
    assert _last(out, "amount_multiple") is None


def test_change_5d():
    out = classify_capital_series(_series([100.0] * 5 + [110.0] * 5))
    assert _last(out, "amount_change_5d") == 0.1
    assert out["amount_change_5d"][8] is None  # 不足 10 行


def test_percentile_needs_60_samples():
    amounts = [100.0 + i for i in range(76)]  # 倍数自下标 15 起有值
    out = classify_capital_series(_series(amounts))
    assert out["amount_multiple_pctl"][74] is None  # 此前只有 59 个有效倍数
    assert out["amount_multiple_pctl"][75] is not None


def test_ties_use_mid_rank():
    out = classify_capital_series(_series([100.0] * 120))
    assert _last(out, "amount_multiple_pctl") == 50.0
    assert _last(out, "capital_volume") == VOLUME_FLAT
    assert _last(out, "capital_concentration") == CONC_NEUTRAL


def test_volume_and_flag_labels():
    base = [100.0 + (i % 7) for i in range(120)]  # 倍数有小幅波动
    stall = classify_capital_series(_series([*base, 200.0], medians=[*[0.001] * 120, -0.002]))
    assert _last(stall, "capital_volume") == VOLUME_EXPAND
    assert _last(stall, "capital_flag") == FLAG_VOLUME_STALL
    advance = classify_capital_series(_series([*base, 200.0]))
    assert _last(advance, "capital_flag") is None
    shrink = classify_capital_series(_series([*base, 50.0]))
    assert _last(shrink, "capital_volume") == VOLUME_SHRINK
    assert _last(shrink, "capital_flag") == FLAG_SHRINK_ADVANCE


def test_concentration_labels():
    shares = [0.40 + 0.001 * (i % 7) for i in range(120)]

    def label(last):
        return _last(classify_capital_series(_series([100.0] * 121, shares=[*shares, last])),
                     "capital_concentration")

    assert label(0.60) == CONC_CONCENTRATED
    assert label(0.20) == CONC_DISPERSED
    assert label(None) is None


def test_later_rows_never_change_earlier_labels():
    amounts = [100.0 + (i * 37 % 23) for i in range(200)]
    full = classify_capital_series(_series(amounts))
    head = classify_capital_series(_series(amounts[:150]))
    assert_frame_equal(full.head(150), head)


def test_missing_required_column_raises():
    with pytest.raises(ValueError):
        classify_capital_series(_series([100.0] * 5).drop("amount_valid_count"))


def test_null_count_rows_get_null_outputs_and_keep_other_columns():
    df = _series([100.0] * 30, counts=[None] * 30).with_columns(pl.lit("keep").alias("extra"))
    out = classify_capital_series(df)
    assert out["extra"].to_list() == ["keep"] * 30
    assert all(out[c].null_count() == 30 for c in DERIVED_COLUMNS)
