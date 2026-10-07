from copy import deepcopy
from datetime import datetime, timedelta

from app.market_time import CN_TZ
from app.services.market_game_adaptation import analyze_adaptation
from app.services.market_game_execution_models import ExecutionEvaluation, ExecutionPolicy


def evaluation(day, value=.01, *, symbol="600000.SH", theme="电子", identity="one"):
    start = datetime.fromisoformat(day).replace(hour=9, minute=33, tzinfo=CN_TZ)
    end = (start + timedelta(days=1)).replace(hour=14, minute=55)
    observed = end.replace(hour=16, minute=0)
    labels = [{"horizon": 1, "trade_date": start.date().isoformat(), "state": "not_executable", "mature": True, "reason": "T+1"},
              {"horizon": 2, "trade_date": end.date().isoformat(), "state": "exited", "mature": True,
               "exit_time": end.isoformat(), "exit_price": 10 * (1 + value), "net_return": value,
               "outcome_available_at": (end + timedelta(minutes=1)).isoformat(), "reason": "模拟"},
              {"horizon": 3, "trade_date": (end + timedelta(days=1)).date().isoformat(), "state": "pending", "reason": "等待"}]
    return ExecutionEvaluation.model_validate({
        "report_id": identity, "evaluated_at": observed.isoformat(), "report_created_at": (start - timedelta(days=1)).isoformat(),
        "input_version": "one", "rule_version": "1.2.0", "status": "partial", "costs": ExecutionPolicy().costs.model_dump(),
        "rows": [{"symbol": symbol, "name": "样本", "mode": "trend_pullback", "sector": theme, "regime": "主升",
                  "signal_time": start.isoformat(), "entry_time": start.isoformat(), "entry_price": 10., "quantity": 1000,
                  "reason": "模拟", "labels": labels}], "limitations": [],
    }).model_dump(mode="json")


def group(result, horizon=2):
    return next(item for item in result["groups"] if item["horizon"] == horizon)


def test_observation_returns_and_unmatured_or_future_labels_are_never_training_data():
    value = evaluation("2026-09-01")
    value["rows"][0]["labels"][1]["mature"] = False
    future = evaluation("2026-10-01")
    result = analyze_adaptation([value, future, {"kind": "observation_only", "rows": [{"close_return": .99}]}],
                                now=datetime(2026, 9, 10, tzinfo=CN_TZ))
    assert result["automatic_adjustment"] is False and result["validation_status"] == "not_validated"
    assert group(result)["sample_size"] == 0 and group(result)["mean_net_return"] is None


def test_repeated_refresh_and_same_theme_cluster_do_not_inflate_independent_dates():
    original = evaluation("2026-09-01", .01)
    second = deepcopy(original)
    second["rows"].append({**deepcopy(second["rows"][0]), "symbol": "600001.SH"})
    result = analyze_adaptation([original, second], now=datetime(2026, 10, 1, tzinfo=CN_TZ))
    assert group(result)["sample_size"] == 1
    assert group(result)["independent_dates"] == 1
    assert group(result)["mean_net_return"] == .01


def test_earliest_report_cohort_is_selected_before_observing_fills():
    original = evaluation("2026-09-01")
    original["rows"][0].update(entry_time=None, entry_price=None, quantity=0)
    for label in original["rows"][0]["labels"]:
        label.update(state="no_entry", net_return=None, exit_time=None, exit_price=None)
    later = evaluation("2026-09-01", .9, symbol="600999.SH", identity="two")
    later["report_created_at"] = "2026-08-31T18:00:00+08:00"
    result = analyze_adaptation([original, later], now=datetime(2026, 10, 1, tzinfo=CN_TZ))
    assert group(result)["sample_size"] == 0


def test_cost_and_rule_versions_have_distinct_cohorts_and_ui_ids():
    first = evaluation("2026-09-01")
    second = evaluation("2026-09-01", .03, identity="two")
    second["rule_version"] = "2.0.0"
    result = analyze_adaptation([first, second], now=datetime(2026, 10, 1, tzinfo=CN_TZ))
    groups = [item for item in result["groups"] if item["horizon"] == 2]
    assert len(groups) == 2 and len({item["id"] for item in groups}) == 2
    assert all(item["sample_size"] == 1 for item in groups)


def test_overlapping_holding_windows_are_purged():
    first = evaluation("2026-09-01")
    second = evaluation("2026-09-02", .8, identity="two")
    result = analyze_adaptation([first, second], now=datetime(2026, 10, 1, tzinfo=CN_TZ))
    assert group(result)["independent_dates"] == 1
    assert group(result)["mean_net_return"] == .01


def test_shadow_walk_forward_uses_only_past_outcomes_with_cluster_embargo():
    begin = datetime(2026, 1, 1)
    samples = [evaluation((begin + timedelta(days=i * 3)).date().isoformat(), -.01 if i < 31 else .5,
                          identity=f"report{i}") for i in range(33)]
    result = analyze_adaptation(samples, now=datetime(2026, 10, 1, tzinfo=CN_TZ))
    tested = group(result)["walk_forward"]
    assert tested["test_dates"] == 2
    assert tested["accepted_dates"] == 0 and tested["withheld_dates"] == 2
    assert tested["fixed_mean_net_return"] == .5
    assert tested["filtered_mean_net_return"] == 0
    assert tested["difference"] == -.5
    assert result["automatic_adjustment"] is False


def test_outcome_availability_not_just_exit_day_controls_admission():
    value = evaluation("2026-09-01")
    value["rows"][0]["labels"][1]["outcome_available_at"] = "2026-10-01T16:00:00+08:00"
    result = analyze_adaptation([value], now=datetime(2026, 9, 10, tzinfo=CN_TZ))
    assert group(result)["sample_size"] == 0


def test_retrospective_labels_cannot_train_folds_before_first_archival():
    begin = datetime(2026, 1, 1)
    samples = [evaluation((begin + timedelta(days=i * 3)).date().isoformat(), identity=f"report{i}")
               for i in range(33)]
    for value in samples:
        value["evaluated_at"] = "2026-09-30T16:00:00+08:00"
    result = analyze_adaptation(samples, now=datetime(2026, 10, 1, tzinfo=CN_TZ))
    assert group(result)["independent_dates"] == 33
    assert group(result)["walk_forward"]["test_dates"] == 0
