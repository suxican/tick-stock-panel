"""Frozen feedback verification cannot leak future data or authorize trading."""
from copy import deepcopy
from datetime import datetime

import pytest

from app.services.market_game_feedback import freeze_feedback, observe_feedback

NOW = datetime.fromisoformat("2026-01-22T10:01:00+08:00")


def report(kind="repair"):
    result = {"as_of": "2026-01-21", "cutoff": "2026-01-21T15:10:00+08:00",
              "created_at": "2026-01-21T15:10:01+08:00",
              "psychology": {"dimensions": [{"id": "risk_appetite", "score": 85 if kind == "continuation" else 20},
                                            {"id": "panic_pressure", "score": 85 if kind == "repair" else 20}]},
              "validity": {"status": "scheduled", "calendar_verified": True,
                           "observation_sessions": ["2026-01-22", "2026-01-23", "2026-01-26"],
                           "expires_at": "2026-01-26T15:00:00+08:00"}}
    result["feedback_hypothesis"] = freeze_feedback(result)
    return result


def behavior():
    return {"status": "ready", "data_date": "2026-01-22", "observed_at": "2026-01-22T10:00:00+08:00",
            "rows": [{"symbol": "000001.SZ", "name": "样本", "status": "ready", "as_of": "2026-01-22T10:00:00+08:00",
                      "participation": {"score": 80, "sample_size": 70}, "support": {"score": 85, "sample_size": 70},
                      "distribution": {"score": 15, "sample_size": 70}, "windows": [{"minutes": 30, "return": .01}],
                      "price_bias": .005}]}


def test_freezes_interpretable_conditions_without_mutating_plan():
    data = report()
    original = deepcopy(data)
    frozen = freeze_feedback(data)
    assert data == original
    assert frozen == data["feedback_hypothesis"]
    assert frozen["kind"] == "repair"
    assert frozen["confirmation"] and frozen["invalidation"]
    assert frozen["shadow_only"] is True and frozen["entry_authorized"] is False


@pytest.mark.parametrize("kind,mechanism", [("repair", "corrective"), ("continuation", "reinforcing")])
def test_rising_price_has_different_feedback_under_frozen_background(kind, mechanism):
    result = observe_feedback(report(kind), behavior(), now=NOW)
    row = result["rows"][0]
    assert row["status"] == "supported"
    assert row["price_direction"] == "up"
    assert row["feedback_type"] == mechanism
    assert result["entry_authorized"] is False
    assert result["scope"] == "frozen_plan_sample"


def test_falling_panic_price_is_self_reinforcing_not_corrective():
    data = behavior()
    row = data["rows"][0]
    row["support"]["score"] = 10
    row["distribution"]["score"] = 90
    row["windows"][0]["return"] = -.01
    row["price_bias"] = -.005
    result = observe_feedback(report(), data, now=NOW)["rows"][0]
    assert result["status"] == "contradicted"
    assert result["price_direction"] == "down"
    assert result["feedback_type"] == "reinforcing"


def test_hot_stall_invalidates_continuation_without_claiming_identity():
    data = behavior()
    data["rows"][0]["distribution"]["score"] = 90
    data["rows"][0]["windows"][0]["return"] = 0
    row = observe_feedback(report("continuation"), data, now=NOW)["rows"][0]
    assert row["status"] == "contradicted"
    assert row["price_direction"] == "flat"
    assert row["feedback_type"] == "unclear"
    assert row["stage"] == "fragile"


@pytest.mark.parametrize("modification", ["no_plan", "unknown_psychology", "no_calendar", "same_day", "future", "short_samples", "missing_return", "naive_bar", "stale", "partial_row", "mixed_date", "envelope_time", "late_creation", "missing_creation"])
def test_incomplete_or_out_of_time_evidence_never_confirms(modification):
    plan, data = report(), behavior()
    row = data["rows"][0]
    if modification == "no_plan":
        plan.pop("feedback_hypothesis")
    elif modification == "unknown_psychology":
        plan["psychology"] = None
        plan["feedback_hypothesis"] = freeze_feedback(plan)
    elif modification == "no_calendar":
        plan["validity"]["calendar_verified"] = False
        plan["feedback_hypothesis"] = freeze_feedback(plan)
    elif modification == "same_day":
        row["as_of"] = "2026-01-21T16:00:00+08:00"
    elif modification == "future":
        row["as_of"] = "2026-01-22T11:00:00+08:00"
    elif modification == "short_samples":
        row["support"]["sample_size"] = 59
    elif modification == "missing_return":
        row["windows"][0]["return"] = None
    elif modification == "naive_bar":
        row["as_of"] = "2026-01-22T10:00:00"
    elif modification == "stale":
        data["status"] = "stale"
    elif modification == "mixed_date":
        data["data_date"] = "2026-01-21"
    elif modification == "envelope_time":
        data["observed_at"] = "2026-01-22T09:59:00+08:00"
    elif modification == "late_creation":
        plan["created_at"] = "2026-01-22T09:30:01+08:00"
    elif modification == "missing_creation":
        plan.pop("created_at")
    else:
        row["status"] = "limited"
    result = observe_feedback(plan, data, now=NOW)
    assert result["rows"][0]["status"] == "unavailable"
    assert result["status"] == "unavailable"


def test_expired_plan_is_not_rolled_forward():
    data = behavior()
    data["rows"][0]["as_of"] = "2026-01-27T10:00:00+08:00"
    data.update(data_date="2026-01-27", observed_at="2026-01-27T10:00:00+08:00")
    row = observe_feedback(report(), data, now=datetime.fromisoformat("2026-01-27T10:01:00+08:00"))["rows"][0]
    assert row["status"] == "expired"


def test_repeat_refresh_preserves_same_observation_and_frozen_background():
    plan, data = report(), behavior()
    original = deepcopy(plan)
    expected = observe_feedback(plan, data, now=NOW)
    later = datetime.fromisoformat("2026-01-22T10:05:00+08:00")
    assert observe_feedback(plan, data, now=later) == expected
    assert plan == original
    plan["psychology"] = {"dimensions": [{"id": "risk_appetite", "score": 100}]}
    assert observe_feedback(plan, data, now=later) == expected


def test_mixed_sample_reports_partial_coverage_without_imputing_missing():
    data = behavior()
    missing = deepcopy(data["rows"][0])
    missing.update(symbol="000002.SZ", status="unavailable")
    data["rows"].append(missing)
    result = observe_feedback(report(), data, now=NOW)
    assert result["status"] == "limited"
    assert [item["status"] for item in result["rows"]] == ["supported", "unavailable"]


def test_naive_request_time_rejected():
    with pytest.raises(ValueError):
        observe_feedback(report(), behavior(), now=NOW.replace(tzinfo=None))
