from copy import deepcopy
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.market_game_execution import (
    evaluate_executions,
    minute_slots,
    simulate_executions,
)
from app.services.market_game_execution_models import ExecutionPolicy
from app.services.market_game_execution_service import _normalize, build_execution_evidence
from app.services.market_game_plan import candidate_conditions

DAYS = ["2026-10-08", "2026-10-09", "2026-10-12"]


def clock(day, value="16:00"):
    return datetime.fromisoformat(f"{day}T{value}:00+08:00")


def report():
    candidate = {"symbol": "600000.SH", "name": "样本", "mode": "trend_pullback", "sector": "电子",
                 "max_position": .1, "trigger_low": 9.9, "trigger_high": 10.5, "invalidation_price": 9.7}
    candidate["conditions"] = candidate_conditions(candidate)
    return {"id": "mg_test", "input_version": "input1", "rule_version": "1.2.0", "as_of": "2026-09-30",
            "cutoff": "2026-09-30T16:00:00+08:00", "created_at": "2026-09-30T16:01:00+08:00",
            "execution_policy": ExecutionPolicy().model_dump(), "market_state": {"phase": "主升"},
            "validity": {"status": "scheduled", "calendar_verified": True, "entry_session": DAYS[0],
                         "observation_sessions": DAYS}, "candidates": [candidate]}


def evidence():
    sessions = {}
    for i, day in enumerate(DAYS):
        price = 10.1 + i * .1
        bars, contexts = [], {}
        for stamp in minute_slots(date.fromisoformat(day)):
            key = stamp.isoformat(timespec="seconds")
            bars.append({"time": key, "open": price, "high": price + .1, "low": price - .1,
                         "close": price, "volume": 50000., "amount": 50000 * 100 * price})
            contexts[key] = {"observed_at": key, "available_at": (stamp + timedelta(minutes=1)).isoformat(timespec="seconds"),
                             "market_coverage": 1, "sector_coverage": 1, "market_allowed": True, "sector_breadth": .8}
        sessions[day] = {"bars": bars, "contexts": contexts, "raw_scale_verified": True,
                         "corporate_action": False, "eligible": True, "limit_up": 11.0, "limit_down": 9.0,
                         "reference_available_at": "2026-09-30T15:00:00+08:00"}
    return {"sessions": {"600000.SH": sessions}}


def test_complete_minute_entry_next_open_t1_and_exact_fee_arithmetic():
    result = simulate_executions(report(), evidence(), now=clock(DAYS[-1]))
    row = result["rows"][0]
    assert result["kind"] == "simulated_execution" and result["entry_authorized"] is False
    assert result["rule_version"] == "1.2.0/exec-1.0.0/policy-1.0.0/eco-legacy"
    assert row["entry_time"] == f"{DAYS[0]}T09:33:00+08:00"
    assert row["signal_time"] == row["entry_time"]
    assert row["entry_price"] == 10.12
    assert row["labels"][0]["state"] == "not_executable" and row["labels"][0]["net_return"] is None
    assert result["status"] == "complete"
    label = row["labels"][1]
    assert label["state"] == "exited" and label["mature"]
    assert label["exit_time"] == f"{DAYS[1]}T14:55:00+08:00"
    assert label["exit_price"] == 10.18
    quantity = row["quantity"]
    buy, sell = row["entry_price"] * quantity, label["exit_price"] * quantity
    expected = (sell - max(5, sell * .0003) - sell * .00051) / (buy + max(5, buy * .0003) + buy * .00001) - 1
    assert label["net_return"] == round(expected, 8)


def test_unfinished_fill_bar_is_never_used():
    result = simulate_executions(report(), evidence(), now=clock(DAYS[0], "09:33"))
    assert result["rows"][0]["entry_time"] is None
    assert result["rows"][0]["labels"][0]["state"] == "pending"


@pytest.mark.parametrize("field,value", [("market_coverage", .99), ("sector_coverage", .99), ("sector_breadth", None)])
def test_incomplete_market_or_sector_cannot_confirm_entry(field, value):
    data = evidence()
    for point in data["sessions"]["600000.SH"][DAYS[0]]["contexts"].values():
        point[field] = value
    row = simulate_executions(report(), data, now=clock(DAYS[-1]))["rows"][0]
    assert row["entry_time"] is None
    assert all(label["state"] == "unavailable" for label in row["labels"])


def test_opening_gap_cancels_even_if_later_price_returns():
    data = evidence()
    data["sessions"]["600000.SH"][DAYS[0]]["bars"][0].update(open=10.6, high=10.7)
    row = simulate_executions(report(), data, now=clock(DAYS[-1]))["rows"][0]
    assert row["entry_time"] is None and row["labels"][2]["state"] == "no_entry"


def test_market_veto_latches_before_price_trigger():
    data = evidence()
    points = data["sessions"]["600000.SH"][DAYS[0]]["contexts"]
    points[f"{DAYS[0]}T09:31:00+08:00"]["market_allowed"] = False
    row = simulate_executions(report(), data, now=clock(DAYS[-1]))["rows"][0]
    assert row["entry_time"] is None and row["labels"][2]["state"] == "no_entry"


def test_gap_minute_cannot_shift_entry_to_later_session():
    data = evidence()
    del data["sessions"]["600000.SH"][DAYS[0]]["bars"][1]
    row = simulate_executions(report(), data, now=clock(DAYS[-1]))["rows"][0]
    assert row["entry_time"] is None and row["labels"][2]["state"] == "unavailable"


def test_stop_on_buy_day_is_deferred_and_gap_exit_not_stop_price():
    data = evidence()
    data["sessions"]["600000.SH"][DAYS[0]]["bars"][4].update(low=9.6)
    data["sessions"]["600000.SH"][DAYS[1]]["bars"][0].update(open=9.4, low=9.3, high=10.3)
    row = simulate_executions(report(), data, now=clock(DAYS[1]))["rows"][0]
    assert row["labels"][0]["state"] == "not_executable"
    assert row["labels"][1]["exit_time"] == f"{DAYS[1]}T09:30:00+08:00"
    assert row["labels"][1]["exit_price"] == 9.39


def test_locked_limit_has_no_exit_and_no_fabricated_profit():
    data = evidence()
    data["sessions"]["600000.SH"][DAYS[0]]["bars"][4].update(low=9.6)
    for bar in data["sessions"]["600000.SH"][DAYS[1]]["bars"]:
        bar.update(open=9., high=9., low=9., close=9., amount=bar["volume"] * 100 * 9)
    label = simulate_executions(report(), data, now=clock(DAYS[1]))["rows"][0]["labels"][1]
    assert label["state"] == "open" and label["net_return"] is None and not label["mature"]


def test_future_defect_does_not_rewrite_earlier_label():
    data = evidence()
    first = simulate_executions(report(), data, now=clock(DAYS[1]))
    data["sessions"]["600000.SH"][DAYS[2]]["corporate_action"] = True
    second = simulate_executions(report(), data, now=clock(DAYS[1]))
    assert first == second
    later = simulate_executions(report(), data, now=clock(DAYS[2]))
    assert later["rows"][0]["entry_time"] == first["rows"][0]["entry_time"]
    assert later["rows"][0]["labels"][1] == first["rows"][0]["labels"][1]
    assert later["rows"][0]["labels"][2]["state"] == "unavailable"


def test_condition_version_and_future_frozen_report_fail_closed():
    value = report()
    value["candidates"][0]["conditions"].append({"id": "new_unknown"})
    assert simulate_executions(value, evidence(), now=clock(DAYS[-1]))["status"] == "unavailable"
    value = report()
    value["created_at"] = f"{DAYS[0]}T10:00:00+08:00"
    assert simulate_executions(value, evidence(), now=clock(DAYS[-1]))["status"] == "unavailable"


def test_repository_adapter_missing_basis_is_explained(tmp_path):
    value = report()
    snapshot = {"coverage": 1, "metadata_scope": "current_observation", "stocks": [
        {"symbol": "600000.SH", "ref_close": 10, "sector": "电子"}]}
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    result = evaluate_executions(repo, value, snapshot, now=clock(DAYS[-1]))
    assert result["status"] == "unavailable"
    assert "原始基准" in result["rows"][0]["reason"]


def test_repository_adapter_runs_full_market_gate_and_minute_fills(tmp_path, monkeypatch):
    from app.services.minute_adjust import mark_minute_basis_raw

    mark_minute_basis_raw(tmp_path)
    data = evidence()
    frame_rows = [{"symbol": "600000.SH", "datetime": datetime.fromisoformat(row["time"]).replace(tzinfo=None),
                   **{key: value for key, value in row.items() if key != "time"}}
                  for day in DAYS for row in data["sessions"]["600000.SH"][day]["bars"]]
    frame = pl.DataFrame(frame_rows)

    class Repo:
        store = SimpleNamespace(data_dir=tmp_path)

        def get_minute_batch(self, symbols, day, asset_type):
            return frame.filter(pl.col("datetime").dt.date() == day)

    monkeypatch.setattr("app.services.market_game_execution_service._closing_evidence", lambda repo, table, symbols, start, end, now: {
        ("600000.SH", start): (10., datetime.combine(start, time(15), CN_TZ), None, None),
    })
    snapshot = {"coverage": 1, "metadata_scope": "current_observation", "stocks": [
        {"symbol": "600000.SH", "ref_close": 10., "sector": "电子", "eligible": True,
         "listing_days": 100, "is_st": False, "limit_up": True}]}
    result = evaluate_executions(Repo(), report(), snapshot, now=clock(DAYS[-1]))
    assert result["rows"][0]["entry_time"] == f"{DAYS[0]}T09:33:00+08:00"
    assert result["rows"][0]["labels"][1]["state"] == "exited"
    record = {"id": "capital1", "report_id": "mg_test", "created_at": f"{DAYS[0]}T09:32:00+08:00",
              "plan_links": [{"symbol": "600000.SH", "status": "watch", "retained_cap": .05}]}
    inputs = build_execution_evidence(Repo(), report(), snapshot, now=clock(DAYS[-1]), observations=[record])
    smaller = simulate_executions(report(), inputs, now=clock(DAYS[-1]))
    assert 0 < smaller["rows"][0]["quantity"] < result["rows"][0]["quantity"]
    record["plan_links"][0].update(status="blocked", retained_cap=0)
    inputs = build_execution_evidence(Repo(), report(), snapshot, now=clock(DAYS[-1]), observations=[record])
    assert simulate_executions(report(), inputs, now=clock(DAYS[-1]))["rows"][0]["entry_time"] is None
    # A real observation created after the 09:33 simulated fill cannot rewrite
    # it, regardless of what that later record says about earlier price action.
    record["created_at"] = f"{DAYS[0]}T09:34:00+08:00"
    inputs = build_execution_evidence(Repo(), report(), snapshot, now=clock(DAYS[-1]), observations=[record])
    assert simulate_executions(report(), inputs, now=clock(DAYS[-1]))["rows"][0]["entry_time"] == result["rows"][0]["entry_time"]
    # A missing member stops the full-universe gate even though candidate data
    # itself is complete, preventing the earlier 30-stock sample shortcut.
    snapshot["stocks"].append({"symbol": "600001.SH", "ref_close": 10., "sector": "电子"})
    result = evaluate_executions(Repo(), report(), snapshot, now=clock(DAYS[-1]))
    assert result["rows"][0]["entry_time"] is None
    assert result["rows"][0]["labels"][1]["state"] == "unavailable"


def test_injected_evidence_does_not_mutate_frozen_plan():
    original = report()
    frozen = deepcopy(original)
    evaluate_executions(None, original, {}, now=clock(DAYS[-1]), evidence=evidence())
    assert original == frozen


def test_adapter_incomplete_frozen_universe_does_not_read_market():
    result = build_execution_evidence(None, report(), {"coverage": .99, "stocks": []}, now=clock(DAYS[-1]))
    assert "覆盖不完整" in result["error"]


def test_ambiguous_0931_start_is_not_shifted_backwards():
    rows = evidence()["sessions"]["600000.SH"][DAYS[0]]["bars"][1:]
    frame = pl.DataFrame([{**{key: value for key, value in row.items() if key != "time"},
                           "datetime": datetime.fromisoformat(row["time"]).replace(tzinfo=None), "symbol": "600000.SH"} for row in rows])
    assert _normalize(frame, date.fromisoformat(DAYS[0]), clock(DAYS[0])).is_empty()


def test_star_market_initial_order_below_200_shares_is_not_filled():
    value, data = report(), evidence()
    value["execution_policy"]["costs"]["account_equity"] = 20_000
    ordinary = simulate_executions(value, data, now=clock(DAYS[-1]))
    assert ordinary["rows"][0]["quantity"] == 100
    value["candidates"][0]["symbol"] = "688001.SH"
    data["sessions"]["688001.SH"] = data["sessions"].pop("600000.SH")
    star = simulate_executions(value, data, now=clock(DAYS[-1]))
    assert star["rows"][0]["quantity"] == 0
    assert star["rows"][0]["labels"][2]["state"] == "no_entry"


def test_excess_capital_history_fails_closed_without_silent_truncation():
    result = build_execution_evidence(None, report(), {}, now=clock(DAYS[-1]), observations=[{}] * 401)
    assert "400" in result["error"]
