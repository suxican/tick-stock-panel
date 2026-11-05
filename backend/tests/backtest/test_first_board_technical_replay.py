from copy import deepcopy

import pytest

from app.backtest import first_board_execution as execution
from app.services.first_board_validation_records import _TechnicalComparison


def recorded(symbol, score=70., *, ma=False, macd=False, pattern="platform",
             day="2026-10-19", previous="2026-10-16", clock="10:00", gain=0., exit_session=1):
    stamp = f"{day}T{clock}:12+08:00"
    shadow = {"version": "ma-macd-shadow-v1", "pattern": pattern, "status": "ready",
              "reference_date": previous, "ma_support": ma, "macd_support": macd}
    candidate = {"pattern": pattern, "can_buy": True, "state": "approaching",
                 "priority_score": {"version": "priority-v1", "status": "partial",
                                    "score": score, "known_max": 90, "parts": {"popularity": {"points": None}}},
                 "evidence": {"reference_date": previous, "technical_shadow": shadow}}
    trade = execution._trade({"observed_at": stamp}, {"symbol": symbol}, 7, [candidate], shadow=False)
    trade.update(status="closed", net_return=gain, gross_return=gain + .001, net_pnl=gain * 10000,
                 entry_at=stamp, _touched=True, _sealed=gain > 0, _exit_sessions=exit_session,
                 _reference_session=previous)
    return trade


def group(report, variant, n=3, window="all"):
    return next(g for g in report["groups"] if (g["variant"], g["top_n"], g["window"]) == (variant, n, window))


def test_same_time_same_pattern_near_scores_only_use_real_replacements_and_t1_costed_outcomes():
    trades = [
        recorded("A", 30, clock="09:55"),  # already occupied a slot
        recorded("B", 73, gain=.01, exit_session=2),  # own band, remains second
        recorded("C", 70, gain=-.03),  # top 3 boundary
        recorded("D", 69, ma=True, gain=.04),
        recorded("E", 68, macd=True, gain=-.05),
        recorded("F", 67.9, ma=True, macd=True, gain=.80),  # outside C's two-point band
        recorded("G", 69.5, ma=True, macd=True, pattern="trend", gain=.30),  # its original slot stays fourth
        recorded("H", 90, ma=True, macd=True, clock="10:01", gain=.90),
    ]
    report = execution._technical_comparison(list(reversed(trades)))
    _TechnicalComparison.model_validate(report)
    assert report["holdout_start"] is None
    assert report["eligible"] == report["priority_known"] == report["scored"] == 8
    base, ma, macd, both = (group(report, variant) for variant in ("baseline", "ma", "macd", "combined"))
    assert (base["replacements"], base["net_pnl_sum"]) == (0, pytest.approx(-200))
    assert (ma["changed_days"], ma["replacements"], ma["sealed"]) == (1, 1, 2)
    assert (macd["net_pnl_sum"], both["net_pnl_sum"]) == (pytest.approx(-400), pytest.approx(500))
    assert ma["t1_closed"] == 2 and ma["t1_net_return_mean"] == pytest.approx(.02)
    assert ma["signals"] == ma["filled"] == 3 and ma["opportunity_days"] == 1
    assert group(report, "ma", n=5)["replacements"] == 0
    assert group(report, "macd", n=5)["replacements"] == 1
    assert group(report, "baseline", window="holdout")["signals"] == 0
    # The strong trend stock occupied slot four. Segmenting after selection
    # never gives each pattern its own top-three allocation.
    assert ma["by_pattern"]["trend"]["signals"] == 0
    assert ma["by_pattern"]["platform"]["replacements"] == ma["replacements"]
    trend = group(report, "macd", n=5)["by_pattern"]["trend"]
    assert (trend["signals"], trend["replacements"], trend["t1_closed"]) == (1, 0, 1)
    assert trend["t1_net_return_mean"] == pytest.approx(.30)


def test_technical_report_shares_complete_cohort_and_holds_out_last_trading_dates():
    trades = [recorded("A", day=day, previous=previous) for day, previous in [
        ("2026-10-19", "2026-10-16"), ("2026-10-20", "2026-10-19"), ("2026-10-21", "2026-10-20"),
        ("2026-10-22", "2026-10-21")]]
    partial, invalid, legacy = (recorded(name) for name in ("P", "I", "L"))
    partial["_candidates"][0]["evidence"]["technical_shadow"].update(status="partial", ma_support=None)
    invalid["_candidates"][0]["evidence"]["technical_shadow"]["ma_support"] = 1
    legacy["_candidates"][0]["evidence"].pop("technical_shadow")
    report = execution._technical_comparison([*trades, partial, invalid, legacy])
    assert report["holdout_start"] == "2026-10-21"
    assert (report["eligible"], report["priority_known"], report["scored"], report["unscored"]) == (7, 7, 4, 3)
    assert report["ma_known"] == 4 and report["macd_known"] == 5
    assert all(row["signals"] == (4 if row["window"] == "all" else 2) for row in report["groups"])
    assert all(row["opportunity_days"] == row["replacements"] == 0 for row in report["groups"])


@pytest.mark.parametrize("damage", ["version", "future", "old_reference", "pattern", "no_calendar", "unavailable"])
def test_technical_replay_excludes_unverifiable_evidence(damage):
    trade = recorded("A")
    shadow = trade["_candidates"][0]["evidence"]["technical_shadow"]
    if damage == "version":
        shadow["version"] = "unknown"
    elif damage == "future":
        shadow["reference_date"] = trade["date"]
    elif damage == "old_reference":
        shadow["reference_date"] = "2026-10-15"
    elif damage == "pattern":
        shadow["pattern"] = "trend"
    elif damage == "no_calendar":
        del trade["_reference_session"]
    else:
        shadow["status"] = "unavailable"
    assert execution._technical_signals(trade) == (None, None)


def test_multi_pattern_uses_same_baseline_winning_candidate_even_when_technical_data_missing():
    first = recorded("A", 70, pattern="platform")["_candidates"][0]
    first["evidence"].pop("technical_shadow")
    second = recorded("A", 65, pattern="trend", ma=True, macd=True)["_candidates"][0]
    trial = execution._trade({"observed_at": "2026-10-19T10:00:12+08:00"}, {"symbol": "A"},
                             7, [second, first], shadow=False)
    trial["_reference_session"] = "2026-10-16"
    assert execution._technical_signals(trial) == (None, None)
    old = deepcopy(first)
    old.pop("priority_score")
    trial = execution._trade({"observed_at": "2026-10-19T10:00:12+08:00"}, {"symbol": "A"},
                             7, [old], shadow=False)
    report = execution._technical_comparison([trial])
    assert report["eligible"] == 1 and report["scored"] == report["priority_known"] == 0
