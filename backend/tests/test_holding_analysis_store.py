"""Research snapshots stay separate from inventory and survive failed writes."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier

import pytest

from app.market_time import CN_TZ
from app.services import holding_analysis_store as storage
from app.services.holding_analysis_store import HoldingAnalysisStore, ResearchError


@pytest.fixture(autouse=True)
def beijing_clock(monkeypatch):
    monkeypatch.setattr(storage, "cn_now", lambda: datetime(2026, 11, 5, 18, 0, tzinfo=CN_TZ))


def _report(*, source="lots", account_id=None, key="lots:one", **overrides):
    report = {
        "source": source,
        "account_id": account_id,
        "input_version": "inventory-v1",
        "profile_version": "profile-v1",
        "generated_at": "2026-11-05T18:00:00+08:00",
        "as_of_date": "2026-11-05",
        "as_of": "2026-11-05T15:00:00+08:00",
        "rows": [{
            "key": key,
            "symbol": "000001.SZ",
            "plan": {"can_adopt": True, "expires_on": "2026-11-06", "actions": []},
        }],
    }
    report.update(overrides)
    return report


def _path(tmp_path):
    return tmp_path / "user_data" / "holding_analysis" / "state.json"


def _saved_plan(store):
    report = store.save_report(_report(profile_version=store.profile_version(["lots:one"])))
    return report, store.adopt_plan(report["id"], "lots:one", "inventory-v1")


def test_profiles_roundtrip_version_hash_and_detached_values(tmp_path):
    store = HoldingAnalysisStore(tmp_path)
    assert store.profiles() == {}
    empty_version = store.profile_version(["one", "two"])
    profile = {"horizon": "swing", "reasons": ["sector"]}
    saved = store.save_profile("one", profile, expected_version=0)
    assert saved == {"profile": profile, "version": 1}
    profile["reasons"].append("external mutation")
    saved["profile"]["horizon"] = "changed"
    assert store.profiles()["one"]["profile"] == {"horizon": "swing", "reasons": ["sector"]}
    assert store.profile_version(["one", "two"]) != empty_version
    assert store.profile_version(["one", "two"]) == store.profile_version(["two", "one", "one"])
    related_version = store.profile_version(["one"])
    store.save_profile("unrelated", {}, expected_version=0)
    assert store.profile_version(["one"]) == related_version
    copy = store.profiles()
    copy.clear()
    assert set(store.profiles()) == {"one", "unrelated"}


def test_profile_compare_and_swap_between_store_instances(tmp_path):
    barrier = Barrier(2)

    def write(value):
        store = HoldingAnalysisStore(tmp_path)
        barrier.wait()
        try:
            return store.save_profile("same", {"value": value}, expected_version=0)
        except ResearchError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, [1, 2]))
    failures = [result for result in results if isinstance(result, ResearchError)]
    assert len(failures) == 1
    assert failures[0].status_code == 409
    assert HoldingAnalysisStore(tmp_path).profiles()["same"]["version"] == 1


def test_concurrent_independent_profiles_do_not_lose_updates(tmp_path):
    def write(index):
        return HoldingAnalysisStore(tmp_path).save_profile(str(index), {"value": index}, 0)

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert len(list(executor.map(write, range(24)))) == 24
    assert len(HoldingAnalysisStore(tmp_path).profiles()) == 24


def test_reports_are_account_and_source_isolated_and_detached(tmp_path):
    store = HoldingAnalysisStore(tmp_path)
    original = _report()
    saved = store.save_report(original)
    store.save_report(_report(source="position_t", account_id="cash-a"))
    b = store.save_report(_report(source="position_t", account_id="cash-b"))
    original["rows"][0]["symbol"] = "changed"
    saved["rows"].clear()
    assert store.get_report(saved["id"])["rows"][0]["symbol"] == "000001.SZ"
    assert store.latest_report("lots", None)["id"] == saved["id"]
    assert store.latest_report("position_t", "cash-b")["id"] == b["id"]
    assert store.latest_report("position_t", "unknown") is None
    assert store.latest_report("lots", "cash-a") is None
    assert datetime.fromisoformat(store.get_report(saved["id"])["created_at"]).utcoffset().total_seconds() == 28800


def test_adoption_is_atomic_idempotent_and_preserves_evidence(tmp_path):
    store = HoldingAnalysisStore(tmp_path)
    report = store.save_report(_report(profile_version=store.profile_version(["lots:one"])))

    def adopt(_):
        return HoldingAnalysisStore(tmp_path).adopt_plan(report["id"], "lots:one", "inventory-v1")

    with ThreadPoolExecutor(max_workers=6) as executor:
        plans = list(executor.map(adopt, range(12)))
    assert len({plan["id"] for plan in plans}) == 1
    assert len(store.list_plans("lots", None)) == 1
    plan = plans[0]
    assert plan["status"] == "adopted" and plan["version"] == 1
    assert plan["row"] == report["rows"][0]
    assert plan["report_id"] == report["id"]
    assert plan["expires_on"] == "2026-11-06"
    assert plan["generated_at"] == report["generated_at"]
    assert plan["as_of_date"] == report["as_of_date"]
    assert plan["profile_keys"] == ["lots:one"]
    plan["row"]["symbol"] = "changed"
    assert store.get_plan(plan["id"])["row"]["symbol"] == "000001.SZ"
    assert store.list_plans("position_t", None) == []
    assert store.list_plans("lots", "cash-a") == []
    all_plans = store.all_plans()
    all_plans[0]["row"]["symbol"] = "mutated"
    assert store.all_plans()[0]["row"]["symbol"] == "000001.SZ"


@pytest.mark.parametrize("failure", ["stale", "missing_row", "not_adoptable", "expired", "missing_expiry"])
def test_adoption_rejects_invalid_report_without_writing_plan(tmp_path, failure):
    store = HoldingAnalysisStore(tmp_path)
    report = _report(profile_version=store.profile_version(["lots:one"]))
    if failure == "not_adoptable":
        report["rows"][0]["plan"]["can_adopt"] = False
    elif failure == "expired":
        report["rows"][0]["plan"]["expires_on"] = "2026-11-04"
    elif failure == "missing_expiry":
        del report["rows"][0]["plan"]["expires_on"]
    saved = store.save_report(report)
    with pytest.raises(ResearchError) as caught:
        store.adopt_plan(
            saved["id"], "unknown" if failure == "missing_row" else "lots:one",
            "stale" if failure == "stale" else "inventory-v1",
        )
    assert caught.value.status_code in {404, 409, 422}
    assert store.list_plans("lots", None) == []


def test_plan_version_alert_deduplication_and_invalidation(tmp_path):
    store = HoldingAnalysisStore(tmp_path)
    report, plan = _saved_plan(store)
    linked = store.set_alert_rule(plan["id"], expected_version=1, rule_id="rule-one")
    assert linked["version"] == 2 and linked["alert_rule_ids"] == ["rule-one"]
    assert linked["alert_bindings"] == {"rule-one": "pending"}
    assert store.set_alert_rule(plan["id"], 2, "rule-one") == linked
    with pytest.raises(ResearchError) as caught:
        store.set_alert_rule(plan["id"], 1, "rule-two")
    assert caught.value.status_code == 409
    invalidated = store.invalidate_plan(plan["id"], expected_version=2)
    assert invalidated["version"] == 3 and invalidated["status"] == "invalidated"
    assert invalidated["alert_rule_ids"] == ["rule-one"]
    with pytest.raises(ResearchError):
        store.set_alert_rule(plan["id"], 3, "rule-two")
    with pytest.raises(ResearchError):
        store.adopt_plan(report["id"], "lots:one", "inventory-v1")


def test_alert_activation_is_versioned_and_rebinding_revokes_active_state(tmp_path):
    store = HoldingAnalysisStore(tmp_path)
    _, plan = _saved_plan(store)
    pending = store.set_alert_rule(plan["id"], 1, "rule-one")
    with pytest.raises(ResearchError) as caught:
        store.activate_alert_rule(plan["id"], 1, "rule-one")
    assert caught.value.status_code == 409
    active = store.activate_alert_rule(plan["id"], pending["version"], "rule-one")
    assert active["version"] == 3
    assert active["alert_bindings"] == {"rule-one": "active"}
    with pytest.raises(ResearchError):
        store.activate_alert_rule(plan["id"], active["version"], "rule-one")
    rebound = store.set_alert_rule(plan["id"], active["version"], "rule-one")
    assert rebound["version"] == 4
    assert rebound["alert_rule_ids"] == ["rule-one"]
    assert rebound["alert_bindings"] == {"rule-one": "pending"}
    assert store.set_alert_rule(plan["id"], 4, "rule-one") == rebound
    assert HoldingAnalysisStore(tmp_path).get_plan(plan["id"])["alert_bindings"] == {"rule-one": "pending"}


def test_alert_cannot_activate_without_durable_pending_state(tmp_path):
    store = HoldingAnalysisStore(tmp_path)
    _, plan = _saved_plan(store)
    with pytest.raises(ResearchError) as caught:
        store.activate_alert_rule(plan["id"], 1, "unknown")
    assert caught.value.status_code == 409
    assert store.get_plan(plan["id"])["version"] == 1


def test_legacy_alert_ids_are_readable_but_require_explicit_pending_transition(tmp_path):
    store = HoldingAnalysisStore(tmp_path)
    _, plan = _saved_plan(store)
    pending = store.set_alert_rule(plan["id"], 1, "rule-one")
    path = _path(tmp_path)
    state = json.loads(path.read_text(encoding="utf-8"))
    del state["plans"][0]["alert_bindings"]
    path.write_text(json.dumps(state), encoding="utf-8")
    assert store.get_plan(plan["id"]).get("alert_bindings", {}) == {}
    with pytest.raises(ResearchError):
        store.activate_alert_rule(plan["id"], pending["version"], "rule-one")
    rebound = store.set_alert_rule(plan["id"], pending["version"], "rule-one")
    assert rebound["version"] == 3
    assert rebound["alert_bindings"] == {"rule-one": "pending"}


def test_failed_activation_keeps_durable_pending_for_restart(tmp_path, monkeypatch):
    store = HoldingAnalysisStore(tmp_path)
    _, plan = _saved_plan(store)
    pending = store.set_alert_rule(plan["id"], 1, "rule-one")

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(storage, "atomic_write_text", fail)
    with pytest.raises(ResearchError):
        store.activate_alert_rule(plan["id"], pending["version"], "rule-one")
    restarted = HoldingAnalysisStore(tmp_path).get_plan(plan["id"])
    assert restarted["version"] == pending["version"]
    assert restarted["alert_bindings"] == {"rule-one": "pending"}


def test_report_retention_preserves_adopted_plan_row(tmp_path):
    store = HoldingAnalysisStore(tmp_path)
    report, plan = _saved_plan(store)
    for index in range(105):
        store.save_report(_report(key=f"row:{index}"))
    raw = json.loads(_path(tmp_path).read_text(encoding="utf-8"))
    assert len(raw["reports"]) == 100
    assert store.get_plan(plan["id"])["row"] == report["rows"][0]
    assert store.adopt_plan(report["id"], "lots:one", "inventory-v1")["id"] == plan["id"]


def test_profile_change_prevents_initial_and_repeated_adoption(tmp_path):
    store = HoldingAnalysisStore(tmp_path)
    report, plan = _saved_plan(store)
    store.save_profile("lots:one", {"horizon": "swing"}, 0)
    with pytest.raises(ResearchError) as caught:
        store.adopt_plan(report["id"], "lots:one", "inventory-v1")
    assert caught.value.code == "stale_profile"
    assert store.get_plan(plan["id"])["version"] == 1
    fresh = store.save_report(_report(profile_version=store.profile_version(["lots:one"])))
    store.save_profile("lots:one", {"horizon": "short"}, 1)
    with pytest.raises(ResearchError) as caught:
        store.adopt_plan(fresh["id"], "lots:one", "inventory-v1")
    assert caught.value.code == "stale_profile"
    assert len(store.all_plans()) == 1


def test_adopted_row_tracks_profiles_of_entire_original_batch(tmp_path):
    store = HoldingAnalysisStore(tmp_path)
    report = _report(profile_version=store.profile_version(["lots:one", "lots:two"]))
    second = _report(key="lots:two")["rows"][0]
    report["rows"].append(second)
    saved = store.save_report(report)
    plan = store.adopt_plan(saved["id"], "lots:one", "inventory-v1")
    assert plan["profile_keys"] == ["lots:one", "lots:two"]
    store.save_profile("lots:two", {"thesis": "updated"}, 0)
    with pytest.raises(ResearchError) as caught:
        store.adopt_plan(saved["id"], "lots:one", "inventory-v1")
    assert caught.value.code == "stale_profile"


def test_idempotent_adoption_cannot_bypass_expiry(tmp_path, monkeypatch):
    store = HoldingAnalysisStore(tmp_path)
    report, plan = _saved_plan(store)
    monkeypatch.setattr(storage, "cn_now", lambda: datetime(2026, 11, 7, 1, 0, tzinfo=CN_TZ))
    with pytest.raises(ResearchError) as caught:
        store.adopt_plan(report["id"], "lots:one", "inventory-v1")
    assert caught.value.code == "expired_report"
    assert store.get_plan(plan["id"])["version"] == 1


def test_new_report_cannot_duplicate_active_plan_for_same_holding(tmp_path):
    store = HoldingAnalysisStore(tmp_path)
    first_report, old_plan = _saved_plan(store)
    second_report = store.save_report(_report(profile_version=store.profile_version(["lots:one"])))
    with pytest.raises(ResearchError) as caught:
        store.adopt_plan(second_report["id"], "lots:one", "inventory-v1")
    assert caught.value.code == "active_plan_exists"
    assert caught.value.status_code == 409
    assert store.adopt_plan(first_report["id"], "lots:one", "inventory-v1")["id"] == old_plan["id"]
    store.invalidate_plan(old_plan["id"], old_plan["version"])
    new_plan = store.adopt_plan(second_report["id"], "lots:one", "inventory-v1")
    assert new_plan["id"] != old_plan["id"]
    assert len(store.all_plans()) == 2


def test_active_plan_deduplication_keeps_account_boundaries(tmp_path):
    store = HoldingAnalysisStore(tmp_path)
    _, original = _saved_plan(store)
    report = store.save_report(_report(source="position_t", account_id="cash-a",
                                      profile_version=store.profile_version(["lots:one"])))
    other = store.adopt_plan(report["id"], "lots:one", "inventory-v1")
    assert original["id"] != other["id"]
    assert len(store.list_plans("lots", None)) == 1
    assert len(store.list_plans("position_t", "cash-a")) == 1


@pytest.mark.parametrize("raw", ["{bad JSON", "[]", "null", '{"schema_version":1}',
                                   '{"schema_version":1,"profiles":[],"reports":[],"plans":[]}'])
def test_corrupt_state_is_never_silently_overwritten(tmp_path, raw):
    path = _path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(raw, encoding="utf-8")
    store = HoldingAnalysisStore(tmp_path)
    with pytest.raises(ResearchError):
        store.profiles()
    with pytest.raises(ResearchError):
        store.save_profile("new", {}, 0)
    assert path.read_text(encoding="utf-8") == raw


def test_future_schema_refuses_writes_and_keeps_original(tmp_path):
    path = _path(tmp_path)
    path.parent.mkdir(parents=True)
    raw = json.dumps({"schema_version": 2, "profiles": {}, "reports": [], "plans": [], "future": True})
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ResearchError) as caught:
        HoldingAnalysisStore(tmp_path).save_report(_report())
    assert caught.value.code == "unsupported_schema"
    assert caught.value.status_code == 409
    assert path.read_text(encoding="utf-8") == raw


def test_failed_atomic_write_leaves_previous_state_and_no_false_success(tmp_path, monkeypatch):
    store = HoldingAnalysisStore(tmp_path)
    store.save_profile("one", {"value": 1}, 0)
    previous = _path(tmp_path).read_bytes()

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(storage, "atomic_write_text", fail)
    with pytest.raises(ResearchError) as caught:
        store.save_profile("one", {"value": 2}, 1)
    assert caught.value.status_code == 500
    assert _path(tmp_path).read_bytes() == previous
    assert store.profiles()["one"] == {"profile": {"value": 1}, "version": 1}


def test_nonfinite_input_cannot_corrupt_json(tmp_path):
    store = HoldingAnalysisStore(tmp_path)
    with pytest.raises(ResearchError):
        store.save_profile("one", {"risk": float("nan")}, 0)
    assert store.profiles() == {}


def test_corrupt_nested_plan_rejects_unrelated_profile_write(tmp_path):
    store = HoldingAnalysisStore(tmp_path)
    _saved_plan(store)
    path = _path(tmp_path)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["plans"][0]["status"] = ["adopted"]
    path.write_text(json.dumps(state), encoding="utf-8")
    previous = path.read_bytes()
    with pytest.raises(ResearchError) as caught:
        store.save_profile("one", {}, 0)
    assert caught.value.code == "storage_corrupt"
    assert path.read_bytes() == previous


def test_read_failure_is_explicit_and_cannot_trigger_empty_replacement(tmp_path, monkeypatch):
    store = HoldingAnalysisStore(tmp_path)
    store.save_profile("one", {}, 0)
    original_read = storage.Path.read_text

    def fail(path, *args, **kwargs):
        if path == _path(tmp_path):
            raise PermissionError("no read access")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(storage.Path, "read_text", fail)
    with pytest.raises(ResearchError) as caught:
        store.save_profile("two", {}, 0)
    assert caught.value.code == "storage_io_error"
    monkeypatch.setattr(storage.Path, "read_text", original_read)
    assert set(store.profiles()) == {"one"}


def test_plan_concurrent_versions_cannot_both_commit(tmp_path):
    store = HoldingAnalysisStore(tmp_path)
    _, plan = _saved_plan(store)
    barrier = Barrier(2)

    def update(rule_id):
        barrier.wait()
        try:
            return HoldingAnalysisStore(tmp_path).set_alert_rule(plan["id"], 1, rule_id)
        except ResearchError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(update, ["rule-one", "rule-two"]))
    assert sum(isinstance(result, ResearchError) for result in results) == 1
    final = store.get_plan(plan["id"])
    assert final["version"] == 2
    assert len(final["alert_rule_ids"]) == 1


def test_missing_entities_have_explicit_not_found(tmp_path):
    store = HoldingAnalysisStore(tmp_path)
    for read in (store.get_report, store.get_plan):
        with pytest.raises(ResearchError) as caught:
            read("../../outside")
        assert caught.value.status_code == 404
    assert not _path(tmp_path).exists()
