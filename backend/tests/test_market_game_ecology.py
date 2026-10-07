"""Competition statistics must preserve missingness and archived knowledge."""
from copy import deepcopy
from datetime import date, timedelta

import pytest

from app.services.market_game_ecology import analyze_ecology


def snapshot():
    start = date(2026, 1, 1)
    history = [{"date": (start + timedelta(days=i)).isoformat(), "amount": 100., "stock_count": 4,
                "coverage": 1., "breadth_up": .5} for i in range(21)]
    return {"as_of": history[-1]["date"], "cutoff": history[-1]["date"] + "T15:10:00+08:00",
            "quality": "ready", "metadata_scope": "current_observation", "coverage": 1.,
            "calendar": {"verified": True}, "history": history, "previous": history[-2],
            "metrics": {**history[-1], "amount": 120., "breadth_up": .75},
            "stocks": [{"symbol": f"00000{i}.SZ", "amount": 30., "change_pct": .01 if i < 3 else -.01,
                        "sectors": ["科技", "成长"] if i < 2 else ["价值"]} for i in range(4)]}


def values(ecology):
    return {item["id"]: item["value"] for item in ecology["metrics"]}


def prior_of(data):
    previous = deepcopy(data)
    previous.update(as_of=data["history"][-2]["date"], cutoff=data["history"][-2]["date"] + "T15:10:00+08:00")
    return previous


def test_activity_hhi_and_fractional_membership_preserve_money():
    data = snapshot()
    original = deepcopy(data)
    report = analyze_ecology(data)
    assert data == original
    assert report["state"] == "expanding"
    assert values(report)["amount_activity"] == pytest.approx(1.2)
    assert values(report)["amount_hhi"] == pytest.approx(.25)
    sectors = {item["name"]: item for item in report["sectors"]}
    assert sum(item["amount_share"] for item in sectors.values()) == pytest.approx(1.)
    assert sectors["科技"]["amount_share"] == pytest.approx(.25)
    assert sectors["价值"]["amount_share"] == pytest.approx(.5)
    assert sectors["科技"]["breadth"] == 1.
    assert sectors["价值"]["breadth"] == .5
    assert sectors["科技"]["relative_return"] == pytest.approx(.005)
    assert all(item["share_change"] is None for item in sectors.values())
    assert report["shadow_only"] is True


def test_future_history_and_archives_cannot_change_current_ecology():
    data = snapshot()
    expected = analyze_ecology(data)
    data["history"].append({"date": "2099-01-01", "amount": 1e15, "coverage": 1, "stock_count": 4})
    future = prior_of(snapshot())
    future["cutoff"] = "2099-01-01T15:10:00+08:00"
    assert analyze_ecology(data, [future]) == expected


@pytest.mark.parametrize("field,value", [("amount", None), ("amount", float("nan")), ("coverage", .9), ("stock_count", 3)])
def test_missing_or_incomparable_prior_session_breaks_activity(field, value):
    data = snapshot()
    data["history"][-2][field] = value
    report = analyze_ecology(data)
    assert values(report)["amount_activity"] is None
    assert report["state"] == "unconfirmed"


@pytest.mark.parametrize("modification", ["short", "duplicate", "calendar"])
def test_baseline_needs_twenty_unique_verified_sessions(modification):
    data = snapshot()
    if modification == "short":
        data["history"] = data["history"][1:]
    elif modification == "duplicate":
        data["history"][-2] = data["history"][-3].copy()
    else:
        data["calendar"]["verified"] = False
    assert values(analyze_ecology(data))["amount_activity"] is None


def test_unknown_sector_is_not_silently_dropped_from_denominator():
    data = snapshot()
    data["stocks"][0]["sectors"] = []
    sectors = analyze_ecology(data)["sectors"]
    assert sum(row["amount_share"] for row in sectors) == pytest.approx(1)
    assert next(row for row in sectors if row["name"] == "未映射")["amount_share"] == pytest.approx(.25)


def test_breadth_and_average_return_do_not_depend_on_other_concept_memberships():
    data = snapshot()
    data["stocks"][0].update(sectors=["科技"], change_pct=.03)
    data["stocks"][1].update(sectors=["科技", "成长", "电子"], change_pct=-.01)
    sectors = {item["name"]: item for item in analyze_ecology(data)["sectors"]}
    assert sectors["科技"]["member_count"] == 2
    assert sectors["科技"]["breadth"] == .5
    assert sectors["科技"]["avg_return"] == pytest.approx(.01)
    assert sectors["科技"]["amount_share"] == pytest.approx(1 / 3)
    assert sum(item["amount_share"] for item in sectors.values()) == pytest.approx(1)
    data["stocks"][1]["sectors"] = ["科技"]
    updated = next(item for item in analyze_ecology(data)["sectors"] if item["name"] == "科技")
    assert updated["breadth"] == sectors["科技"]["breadth"]
    assert updated["avg_return"] == sectors["科技"]["avg_return"]


@pytest.mark.parametrize("scope", ["historical_unverified", "unknown"])
def test_historical_replays_never_reuse_current_memberships(scope):
    data = snapshot()
    data["metadata_scope"] = scope
    report = analyze_ecology(data)
    assert report["sectors"] == []
    assert report["membership_version"] is None
    assert values(report)["amount_hhi"] == .25


@pytest.mark.parametrize("modification", ["missing", "negative", "duplicate"])
def test_invalid_stock_amount_does_not_become_zero_or_valid_hhi(modification):
    data = snapshot()
    if modification == "duplicate":
        data["stocks"].append(data["stocks"][0].copy())
    else:
        data["stocks"][0]["amount"] = None if modification == "missing" else -1
    report = analyze_ecology(data)
    assert values(report)["amount_hhi"] is None
    assert report["sectors"] == []


def test_previous_archive_comparison_requires_same_universe_and_memberships():
    data = snapshot()
    prior = prior_of(data)
    prior["stocks"][0]["amount"] = 90.
    report = analyze_ecology(data, [prior])
    assert report["comparison_date"] == prior["as_of"]
    assert values(report)["concentration_change"] == pytest.approx(.25 - 1 / 3)
    assert all(item["share_change"] is not None for item in report["sectors"])
    changed_mapping = deepcopy(prior)
    changed_mapping["stocks"][0]["sectors"] = ["新板块"]
    assert all(item["share_change"] is None for item in analyze_ecology(data, [changed_mapping])["sectors"])
    prior["stocks"][0]["symbol"] = "999999.SZ"
    report = analyze_ecology(data, [prior])
    assert values(report)["concentration_change"] is None
    assert all(item["share_change"] is None for item in report["sectors"])


def test_duplicate_archives_and_skipped_session_are_not_fresh_comparison():
    data = snapshot()
    prior = prior_of(data)
    assert values(analyze_ecology(data, [prior, prior]))["concentration_change"] is None
    prior["as_of"] = data["history"][-3]["date"]
    assert values(analyze_ecology(data, [prior]))["concentration_change"] is None


@pytest.mark.parametrize("coverage", [None, .94])
def test_insufficient_audited_coverage_keeps_state_unconfirmed(coverage):
    data = snapshot()
    data["coverage"] = coverage
    report = analyze_ecology(data)
    assert report["state"] == "unconfirmed"
    assert values(report)["amount_hhi"] is None
    assert values(report)["breadth_up"] is None


def test_real_zero_return_is_flat_but_missing_return_is_unknown():
    data = snapshot()
    data["stocks"][0]["change_pct"] = 0
    sector = next(item for item in analyze_ecology(data)["sectors"] if item["name"] == "科技")
    assert sector["breadth"] == .5
    data["stocks"][0]["change_pct"] = None
    sector = next(item for item in analyze_ecology(data)["sectors"] if item["name"] == "科技")
    assert sector["breadth"] is None
    assert sector["avg_return"] is None


@pytest.mark.parametrize("state,amount,breadth", [("contracting", 80, .3), ("concentrated", 90, .4), ("rotation", 100, .5)])
def test_competition_states_require_joint_evidence(state, amount, breadth):
    data = snapshot()
    prior = prior_of(data)
    data["metrics"].update(amount=amount, breadth_up=breadth)
    data["stocks"][0]["amount"] = 150
    result = analyze_ecology(data, [prior])
    assert result["state"] == state
    if state in {"concentrated", "rotation"}:
        assert analyze_ecology(data)["state"] == "unconfirmed"
