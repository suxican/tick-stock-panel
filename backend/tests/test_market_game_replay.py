"""Daily-bar trade approximation and grouping of the offline stock replay."""
from datetime import date, timedelta

import pytest

from app.services.market_game_replay import ROUND_TRIP_COST, _trade, summarize

D0 = date(2026, 11, 2)


def _bars(days, *, scale=1.0, segment=None):
    """days: list of (open, high, low, close[, locked_down]) on the adjusted scale."""
    data = {key: [] for key in ("date", "open", "high", "low", "close", "raw_close", "raw_high", "raw_low",
                                "signal_limit_up", "signal_limit_down", "_segment_key")}
    for i, bar in enumerate(days):
        o, h, low, c = bar[:4]
        locked = len(bar) > 4 and bar[4]
        data["date"].append(D0 + timedelta(days=i))
        data["open"].append(o)
        data["high"].append(c if locked else h)
        data["low"].append(c if locked else low)
        data["close"].append(c)
        data["raw_close"].append(c * scale)
        data["raw_high"].append((c if locked else h) * scale)
        data["raw_low"].append((c if locked else low) * scale)
        data["signal_limit_up"].append(False)
        data["signal_limit_down"].append(bool(locked))
        data["_segment_key"].append(segment[i] if segment else "A@1")
    return data


CANDIDATE = {"trigger_low": 10.1, "trigger_high": 10.25, "invalidation_price": 9.7, "pressure_price": 11.0}
EW = {D0 + timedelta(days=i): 0.01 for i in range(5)}


def test_open_inside_band_fills_at_open_and_exits_at_day_three_close():
    bars = _bars([(10, 10, 10, 10), (10.2, 10.4, 10.1, 10.3), (10.3, 10.5, 10.2, 10.4), (10.4, 10.6, 10.3, 10.5)])
    result = _trade(bars, 0, CANDIDATE, EW)
    assert result["status"] == "filled" and result["exit"] == "time"
    assert result["net"] == pytest.approx(10.5 / 10.2 - 1 - ROUND_TRIP_COST)
    assert result["fwd1"] == pytest.approx(0.03) and result["fwd3"] == pytest.approx(0.05)
    assert result["ew3"] == pytest.approx(1.01 ** 3 - 1)


def test_low_open_fills_at_trigger_and_gap_above_band_cancels():
    rising = _bars([(10, 10, 10, 10), (9.9, 10.3, 9.8, 10.2), (10.2, 10.3, 10.1, 10.2), (10.2, 10.3, 10.1, 10.2)])
    assert _trade(rising, 0, CANDIDATE, EW)["net"] == pytest.approx(10.2 / 10.1 - 1 - ROUND_TRIP_COST)
    gap = _bars([(10, 10, 10, 10), (10.4, 10.6, 10.3, 10.5), (10.5, 10.6, 10.4, 10.5), (10.5, 10.6, 10.4, 10.5)])
    assert _trade(gap, 0, CANDIDATE, EW)["status"] == "cancelled"
    weak = _bars([(10, 10, 10, 10), (9.9, 10.0, 9.8, 9.9), (9.9, 10, 9.8, 9.9), (9.9, 10, 9.8, 9.9)])
    assert _trade(weak, 0, CANDIDATE, EW)["status"] == "not_triggered"


def test_t_plus_one_stop_target_and_locked_limit_down():
    # Entry-day low below the stop cannot be sold; close below it exits next open.
    broke = _bars([(10, 10, 10, 10), (10.2, 10.3, 9.5, 9.6), (9.4, 9.8, 9.3, 9.5), (9.5, 9.6, 9.4, 9.5)])
    result = _trade(broke, 0, CANDIDATE, EW)
    assert result["exit"] == "stop" and result["net"] == pytest.approx(9.4 / 10.2 - 1 - ROUND_TRIP_COST)
    target = _bars([(10, 10, 10, 10), (10.2, 10.3, 10.1, 10.2), (10.5, 11.2, 10.4, 11.0), (11, 11, 11, 11)])
    assert _trade(target, 0, CANDIDATE, EW)["exit"] == "target"
    both = _bars([(10, 10, 10, 10), (10.2, 10.3, 10.1, 10.2), (10.2, 11.2, 9.6, 10.0), (10, 10, 10, 10)])
    assert _trade(both, 0, CANDIDATE, EW)["exit"] == "stop"  # ambiguous order counts as the stop
    locked = _bars([(10, 10, 10, 10), (10.2, 10.3, 10.1, 10.2), (9.18, 9.18, 9.18, 9.18, True), (9.0, 9.1, 8.9, 9.0)])
    result = _trade(locked, 0, CANDIDATE, EW)
    assert result["exit"] == "stop" and result["net"] == pytest.approx(9.0 / 10.2 - 1 - ROUND_TRIP_COST)


def test_raw_thresholds_convert_to_adjusted_scale_and_gaps_have_no_outcome():
    bars = _bars([(10, 10, 10, 10), (10.2, 10.4, 10.1, 10.3), (10.3, 10.5, 10.2, 10.4), (10.4, 10.6, 10.3, 10.5)], scale=2)
    raw = {key: value * 2 for key, value in CANDIDATE.items()}
    assert _trade(bars, 0, raw, EW)["net"] == pytest.approx(10.5 / 10.2 - 1 - ROUND_TRIP_COST)
    gap = _bars([(10, 10, 10, 10), (10.2, 10.4, 10.1, 10.3), (10.3, 10.5, 10.2, 10.4), (10.4, 10.6, 10.3, 10.5)],
                segment=["A@1", "A@1", "A@2", "A@2"])
    result = _trade(gap, 0, CANDIDATE, EW)
    assert result["status"] == "no_data" and result["fwd3"] is None and result["fwd1"] is not None


def test_summary_groups_by_source_gate_year_and_split():
    rows = [
        {"date": "2022-05-05", "source": "plan", "gate": "none", "fwd1": .01, "fwd3": .02, "ew1": 0, "ew3": .01,
         "status": "filled", "net": .03, "exit": "target"},
        {"date": "2024-05-06", "source": "plan", "gate": "veto", "fwd1": -.01, "fwd3": -.02, "ew1": 0, "ew3": 0,
         "status": "not_triggered", "net": None, "exit": None},
    ]
    groups = {row["group"]: row for row in summarize(rows, split=date(2023, 1, 1))}
    assert groups["plan"]["n"] == 2 and groups["plan"]["fill_rate"] == .5
    assert groups["plan"]["excess3_mean"] == pytest.approx((.01 - .02) / 2)
    assert groups["plan|train"]["trade_mean"] == pytest.approx(.03)
    assert groups["plan|test"]["trade_n"] == 0 and groups["plan|gate:veto"]["n"] == 1
    assert groups["plan|year:2024"]["dates"] == 1
