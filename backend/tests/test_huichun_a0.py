"""A0 signal audit checks; minute fills and portfolio acceptance are out of scope."""

from datetime import date, timedelta

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from app.strategy import huichun_a0 as a0

ORIGIN = date(2020, 1, 1)


def _prices(values, *, eligible=True, segments=None):
    return pl.DataFrame(
        {
            "symbol": ["600001.SH"] * len(values),
            "date": [ORIGIN + timedelta(days=i) for i in range(len(values))],
            "close": [float(v) for v in values],
            "segment_id": segments or [0] * len(values),
            "eligible": pl.Series([eligible] * len(values), dtype=pl.Boolean),
        }
    )


def _controlled(monkeypatch, *, closes=None, q=None, ma=None, lag=None, dif=None, eligible=True):
    """Known indicator observations isolate cycle rules from EMA arithmetic."""
    values = closes or [9, 10, 16, 14, 13, 14]
    frame = _prices(values, eligible=eligible)
    gaps = q or [-1, 1, 1, -1, -1, 1]
    difs = dif or [0.1] * len(values)

    def indicators(source, params):
        n = source.height
        out = source.with_columns(
            pl.Series("dif", difs[:n], dtype=pl.Float64),
            pl.Series("dea", [d - g * 0.01 for d, g in zip(difs, gaps, strict=True)][:n]),
            pl.Series("q", gaps[:n], dtype=pl.Float64),
            pl.Series("ma60", ma or [9.0] * len(values), dtype=pl.Float64).head(n),
            pl.Series("ma60_lag", lag or [8.0] * len(values), dtype=pl.Float64).head(n),
            pl.Series("prior_bars", range(n), dtype=pl.UInt32),
        ).with_columns(
            (pl.max_horizontal(pl.col("dif").abs(), pl.col("dea").abs()) / pl.col("close")).alias(
                "zero_distance"
            ),
            (pl.col("q").abs() / pl.col("close")).alias("gap_distance"),
        )
        return a0._add_crosses(out)

    monkeypatch.setattr(a0, "_indicators", indicators)
    return frame


def _audit(frame, *, start=ORIGIN, end=None, **kwargs):
    return a0.audit_a0(
        frame,
        start=start,
        end=end or frame["date"].max(),
        params=a0.A0Params(warmup_bars=1, **kwargs),
    )


def test_acceptance_01_exact_forty_percent_boundary_passes(monkeypatch):
    result = _audit(_controlled(monkeypatch))
    first = result.cycles.row(0, named=True)
    assert first["g_close"] == 10
    assert first["d_close"] == 14
    assert first["rally_qualified"] is True
    assert first["strict_signal"] is True
    assert result.signals.height == 1


@pytest.mark.parametrize("dead_close,qualified", [(5.81, True), (5.8099999999, False)])
def test_forty_percent_decimal_price_boundary(monkeypatch, dead_close, qualified):
    frame = _controlled(monkeypatch, closes=[4, 4.15, 7, dead_close, 5.5, 10])
    assert _audit(frame).cycles.row(0, named=True)["rally_qualified"] is qualified


@pytest.mark.parametrize("scale", [0.01, 0.37, 1.37, 100.0])
def test_forty_percent_boundary_survives_uniform_adjustment_scale(monkeypatch, scale):
    frame = _controlled(monkeypatch, closes=[v * scale for v in [4, 4.15, 7, 5.81, 5.5, 10]])
    assert _audit(frame).cycles.row(0, named=True)["rally_qualified"] is True


def test_acceptance_02_intraday_peak_does_not_replace_dead_cross_close(monkeypatch):
    frame = _controlled(monkeypatch, closes=[9, 10, 16, 13.5, 13, 14])
    result = _audit(frame.with_columns(pl.lit(16).alias("high")))
    first = result.cycles.row(0, named=True)
    assert first["rally_return"] == pytest.approx(0.35)
    assert first["status"] == "rally_rejected"
    assert result.signals.is_empty()


def test_acceptance_03_only_first_subsequent_cross_can_finish_old_cycle(monkeypatch):
    frame = _controlled(
        monkeypatch,
        closes=[9, 10, 16, 14, 13, 14, 15, 14, 13, 14],
        q=[-1, 1, 1, -1, -1, 1, 1, -1, -1, 1],
        dif=[0.1] * 5 + [2.0] + [0.1] * 4,
    )
    result = _audit(frame)
    first = result.cycles.row(0, named=True)
    assert first["status"] == "first_cross_rejected"
    assert first["r_date"] == ORIGIN + timedelta(days=5)
    assert "zero_distance_exceeded" in first["reasons"]
    assert result.cycles.row(1, named=True)["g_date"] == first["r_date"]
    assert result.signals.is_empty()


def test_acceptance_04_zero_keeps_last_nonzero_sign():
    frame = _prices([10] * 9).with_columns(
        pl.Series("q", [0.0, 1.0, 0.0, 1.0, 0.0, -1.0, 0.0, -1.0, 1.0])
    )
    out = a0._add_crosses(frame)
    assert out["golden_cross"].to_list() == [False] * 8 + [True]
    assert out["dead_cross"].to_list() == [False] * 5 + [True, False, False, False]


def test_acceptance_17_waiting_cycle_has_no_forced_outcome(monkeypatch):
    frame = _controlled(monkeypatch)
    result = _audit(frame.head(5))
    first = result.cycles.row(0, named=True)
    assert first["status"] == "waiting_first_cross"
    assert first["r_date"] is None
    assert first["shape_match"] is None
    assert first["strict_signal"] is None
    assert result.signals.is_empty()


@pytest.mark.parametrize(
    "eligible,status,strict",
    [(True, "signal", True), (False, "ineligible", False), (None, "eligibility_unknown", None)],
)
def test_shape_match_keeps_unknown_or_excluded_eligibility(monkeypatch, eligible, status, strict):
    result = _audit(_controlled(monkeypatch, eligible=eligible))
    signal = result.signals.row(0, named=True)
    assert signal["shape_match"] is True
    assert signal["status"] == status
    assert signal["strict_signal"] is strict


@pytest.mark.parametrize("distance,accepted", [(0.02, True), (0.020001, False)])
def test_zero_axis_boundary_is_inclusive(monkeypatch, distance, accepted):
    frame = _controlled(monkeypatch, dif=[0.1] * 5 + [14 * distance])
    result = _audit(frame)
    assert result.cycles.row(0, named=True)["shape_match"] is accepted


@pytest.mark.parametrize(
    "ma,last_ma,accepted",
    [(14.0, 8.0, False), (13.999, 8.0, True), (9.0, 9.0, False), (9.0, 8.999, True)],
)
def test_price_and_ma_slope_are_strict(monkeypatch, ma, last_ma, accepted):
    frame = _controlled(monkeypatch, ma=[9.0] * 5 + [ma], lag=[8.0] * 5 + [last_ma])
    assert _audit(frame).cycles.row(0, named=True)["shape_match"] is accepted


def test_missing_ma_history_is_explicit(monkeypatch):
    frame = _controlled(monkeypatch, ma=[None] * 6)
    first = _audit(frame).cycles.row(0, named=True)
    assert first["status"] == "first_cross_rejected"
    assert "indicator_history_insufficient" in first["reasons"]


def _reference_ema(values, span):
    alpha = 2 / (span + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def _real_prices():
    return (
        [10.0] * 260
        + [10 - i * 0.1 for i in range(1, 21)]
        + [8 + i * 0.4 for i in range(1, 36)]
        + [22 - i * 0.1 for i in range(1, 61)]
        + [16 + i * 0.25 for i in range(1, 31)]
    )


def test_indicators_match_independent_ema_and_exact_ma():
    values = [10 + (i % 11) * 0.6 + i * 0.01 for i in range(300)]
    result = a0.audit_a0(_prices(values), start=ORIGIN, end=ORIGIN + timedelta(days=299))
    fast, slow = _reference_ema(values, 10), _reference_ema(values, 20)
    dif = [f - s for f, s in zip(fast, slow, strict=True)]
    dea = _reference_ema(dif, 9)
    assert result.indicators["ema10"].to_list() == pytest.approx(fast, abs=1e-12)
    assert result.indicators["ema20"].to_list() == pytest.approx(slow, abs=1e-12)
    assert result.indicators["dea"].to_list() == pytest.approx(dea, abs=1e-12)
    assert result.indicators["q"].to_list() == pytest.approx(
        [d - s for d, s in zip(dif, dea, strict=True)], abs=1e-12
    )
    assert result.indicators["ma60"][59] == pytest.approx(sum(values[:60]) / 60)
    assert result.indicators["ma60_lag"][64] == result.indicators["ma60"][59]
    assert result.indicators["ma60"][58] is None


@pytest.mark.parametrize("prior,expected", [(249, False), (250, True)])
def test_golden_requires_250_prior_valid_bars(monkeypatch, prior, expected):
    n = prior + 1
    frame = _controlled(monkeypatch, closes=[10] * n, q=[-1] * prior + [1])
    result = a0.audit_a0(frame, start=ORIGIN, end=frame["date"].max())
    assert (result.cycles.height == 1) is expected


def test_unknown_gap_ends_old_cycle_and_restarts_indicator_warmup():
    prices = _real_prices()[:320]
    frame = _prices([*prices, 15, 16, 17], segments=[0] * len(prices) + [1] * 3)
    result = a0.audit_a0(frame, start=ORIGIN, end=frame["date"].max())
    gap_cycle = result.cycles.filter(pl.col("status") == "cycle_unknown_gap")
    assert gap_cycle.height == 1
    assert gap_cycle["r_date"][0] is None
    new = result.indicators.filter(pl.col("segment_id") == "1")
    assert new["ema10"][0] == 15
    assert new["dea"][0] == 0
    assert new["prior_bars"].to_list() == [0, 1, 2]
    assert result.cycles.filter(pl.col("segment_id") == "1").is_empty()


def test_future_rows_and_prices_do_not_change_past_signals_or_indicators():
    frame = _prices(_real_prices())
    end = frame["date"][-8]
    changed = frame.with_columns(
        pl.when(pl.col("date") > end).then(1_000_000.0).otherwise(pl.col("close")).alias("close")
    )
    kwargs = {
        "start": ORIGIN,
        "end": end,
        "params": a0.A0Params(ma_window=3, slope_lag=1, zero_threshold=1),
    }
    full = a0.audit_a0(changed, **kwargs)
    prefix = a0.audit_a0(frame.filter(pl.col("date") <= end), **kwargs)
    assert full.signals.height > 0
    assert_frame_equal(full.indicators, prefix.indicators)
    assert_frame_equal(full.cycles, prefix.cycles)
    assert_frame_equal(full.signals, prefix.signals)


def test_trailing_unknown_gap_ends_cycle_without_fabricated_prices():
    frame = _prices(_real_prices()[:320])
    missing_date = frame["date"][-1] + timedelta(days=1)
    annotated = frame.with_columns(
        pl.when(pl.col("date") == frame["date"][-1])
        .then(pl.lit(missing_date))
        .otherwise(None)
        .alias("segment_invalidated_at")
    )
    after_gap = a0.audit_a0(annotated, start=ORIGIN, end=missing_date)
    assert after_gap.cycles["status"].to_list() == ["cycle_unknown_gap"]
    assert after_gap.cycles["audit_date"][0] == missing_date
    assert after_gap.indicators.height == frame.height
    before_gap = a0.audit_a0(annotated, start=ORIGIN, end=frame["date"][-1])
    no_annotation = a0.audit_a0(frame, start=ORIGIN, end=frame["date"][-1])
    assert_frame_equal(before_gap.cycles, no_annotation.cycles)
    assert_frame_equal(before_gap.indicators, no_annotation.indicators)


def test_start_only_limits_output_not_ema_or_cycle_history():
    frame = _prices(_real_prices())
    params = a0.A0Params(ma_window=3, slope_lag=1, zero_threshold=1)
    full = a0.audit_a0(frame, start=ORIGIN, end=frame["date"].max(), params=params)
    start = frame["date"][330]
    later = a0.audit_a0(frame, start=start, end=frame["date"].max(), params=params)
    assert later.signals.height > 0
    assert_frame_equal(later.indicators, full.indicators.filter(pl.col("date") >= start))
    assert_frame_equal(later.cycles, full.cycles.filter(pl.col("audit_date") >= start))
    assert_frame_equal(later.signals, full.signals.filter(pl.col("r_date") >= start))


def test_multiple_symbols_and_shuffled_input_are_independent():
    one = _prices(_real_prices())
    two = one.with_columns(
        pl.lit("000001.SZ").alias("symbol"), (pl.col("close") * 2).alias("close")
    )
    combined = pl.concat([one, two]).reverse()
    params = a0.A0Params(ma_window=3, slope_lag=1, zero_threshold=1)
    got = a0.audit_a0(combined, start=ORIGIN, end=one["date"].max(), params=params)
    expected = a0.audit_a0(one, start=ORIGIN, end=one["date"].max(), params=params)
    assert_frame_equal(got.indicators.filter(pl.col("symbol") == "600001.SH"), expected.indicators)
    assert got.signals.height == 2 * expected.signals.height


@pytest.mark.parametrize("scale", [0.37, 1.37, 100.0])
def test_uniform_adjustment_scale_preserves_cycle_and_shape_decisions(scale):
    frame = _prices(_real_prices())
    params = a0.A0Params(ma_window=3, slope_lag=1, zero_threshold=1)
    kwargs = {"start": ORIGIN, "end": frame["date"].max(), "params": params}
    base = a0.audit_a0(frame, **kwargs)
    scaled = a0.audit_a0(frame.with_columns(pl.col("close") * scale), **kwargs)
    decisions = [
        "g_date",
        "d_date",
        "r_date",
        "rally_qualified",
        "shape_match",
        "strict_signal",
        "status",
    ]
    assert_frame_equal(base.cycles.select(decisions), scaled.cycles.select(decisions))


def test_missing_eligibility_defaults_to_unknown():
    frame = _prices(_real_prices()).drop("eligible", "segment_id")
    result = a0.audit_a0(
        frame,
        start=ORIGIN,
        end=frame["date"].max(),
        params=a0.A0Params(ma_window=3, slope_lag=1, zero_threshold=1),
    )
    assert result.signals.height > 0
    assert result.signals["strict_signal"].null_count() == result.signals.height


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf"), None])
def test_invalid_close_is_rejected_not_silently_skipped(bad):
    frame = _prices([10, 11]).with_columns(pl.Series("close", [10.0, bad]))
    with pytest.raises(ValueError, match="close"):
        _audit(frame)


def test_duplicates_fail_closed():
    frame = _prices([10, 11])
    with pytest.raises(ValueError, match="duplicate"):
        _audit(pl.concat([frame, frame.head(1)]))


def test_reappearing_segment_id_fails_closed():
    frame = _prices([10, 11, 12], segments=[0, 1, 0])
    with pytest.raises(ValueError, match="contiguous"):
        _audit(frame)


def test_empty_input_has_stable_schemas():
    result = a0.audit_a0(_prices([10]).head(0), start=ORIGIN, end=ORIGIN)
    assert result.signals.is_empty()
    assert result.cycles.is_empty()
    assert result.signals.schema["strict_signal"] == pl.Boolean
    assert result.indicators.schema["date"] == pl.Date


@pytest.mark.parametrize(
    "kwargs",
    [
        {"warmup_bars": 0},
        {"zero_threshold": -1},
        {"rally_threshold": float("nan")},
        {"slope_lag": 0},
        {"ma_window": 0},
    ],
)
def test_invalid_parameters_are_rejected(kwargs):
    with pytest.raises(ValueError):
        a0.A0Params(**kwargs)
