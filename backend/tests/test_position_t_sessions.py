from datetime import UTC, timedelta
from unittest.mock import Mock

import pytest
from test_position_t_live_monitor import first_fill, target_quote
from test_position_t_rules import sample_context
from test_position_t_service import accept, triggered
from test_position_t_service import service as service_fixture

from app.services.position_t_store import LedgerError
from app.strategy.position_t import _minute_ends, as_cn


@pytest.fixture
def service(tmp_path):
    return service_fixture.__wrapped__(tmp_path)


@pytest.mark.parametrize("stamp,calendar", [
    ("2026-10-16T09:15:00+08:00", True),
    ("2026-10-16T09:29:59+08:00", True),
    ("2026-10-16T11:30:00+08:00", True),
    ("2026-10-16T12:00:00+08:00", True),
    ("2026-10-16T14:57:00+08:00", True),
    ("2026-10-16T15:00:00+08:00", True),
    ("2026-10-17T10:00:00+08:00", False),
    ("2026-10-01T10:00:00+08:00", False),
    ("2027-01-04T10:00:00+08:00", None),
])
def test_closed_session_skips_quotes_evaluation_and_account_scans(service, monkeypatch, stamp, calendar):
    service.market.now = as_cn(stamp)
    service.market.trading_day = lambda day: calendar
    collect = Mock(side_effect=AssertionError("must not collect"))
    service.market.collect = collect
    monkeypatch.setattr("app.services.position_t_service.evaluate_candidate", Mock(side_effect=AssertionError("must not evaluate")))
    monkeypatch.setattr(service.store, "list_accounts", Mock(side_effect=AssertionError("must not scan accounts")))
    service.run_once()
    assert service.health["status"] == "idle"
    assert service.health["monitored_positions"] == 0
    assert service.health["reasons"]
    collect.assert_not_called()
    assert not service.store.list_signals()
    assert not service.notes


def test_close_expires_opportunities_once_without_touching_actual_position(service, monkeypatch):
    plan = first_fill(service)
    target_quote(service)
    service.run_once()
    before = service.store.get_plan(plan["id"])
    executions = service.store.plan_detail(plan["id"])["executions"]
    service.notes.clear()
    service.market.now = as_cn("2026-10-16T15:00:00+08:00")
    suspend = Mock(wraps=service.store.expire_signals)
    monkeypatch.setattr(service.store, "expire_signals", suspend)
    for _ in range(3):
        service.run_once()
    after = service.store.get_plan(plan["id"])
    for key in ("state", "version", "first_stopped", "reserved_qty", "reserved_cash", "unmatched_qty"):
        assert after[key] == before[key]
    assert after["monitor"]["state"] == "paused"
    assert service.store.plan_detail(plan["id"])["executions"] == executions
    assert suspend.call_count == 1
    assert not service.notes


def test_resume_requires_fresh_evaluation_and_beijing_session(service):
    start = service.market.now
    service.market.now = as_cn("2026-10-16T08:00:00+08:00")
    service.run_once()
    service.market.now = start.astimezone(UTC)
    assert triggered(service)["state"] == "triggered"
    assert service.health["status"] == "monitoring"


@pytest.mark.parametrize("stamp", [
    "2026-10-16T09:30:00+08:00", "2026-10-16T11:29:59+08:00",
    "2026-10-16T13:00:00+08:00", "2026-10-16T14:56:59+08:00",
])
def test_continuous_session_boundaries_allow_collection(service, stamp):
    service.market.now = as_cn(stamp)
    collect = Mock(wraps=service.market.collect)
    service.market.collect = collect
    service.run_once()
    collect.assert_called_once()
    assert service.health["monitored_positions"] == 1


def test_lunch_resume_does_not_revive_old_opportunity(service):
    original = triggered(service)
    service.market.now = as_cn("2026-10-16T11:30:00+08:00")
    service.run_once()
    assert all(s["state"] == "expired" for s in service.store.list_signals())
    service.market.now = as_cn("2026-10-16T13:00:00+08:00")
    collect = Mock(wraps=service.market.collect)
    service.market.collect = collect
    service.notes.clear()
    service.run_once()
    collect.assert_called_once()
    assert all(s["state"] != "triggered" for s in service.store.list_signals())
    assert all(s["id"] != original["id"] for s in service.store.list_signals())
    assert not service.notes


def test_collect_crossing_lunch_discards_result_and_does_not_archive(service, monkeypatch):
    service.market.now = as_cn("2026-10-16T11:29:59+08:00")
    original = service.market.collect
    def collect(*args):
        result = original(*args)
        service.market.now += timedelta(seconds=1)
        return result
    service.market.collect = collect
    save = Mock(side_effect=AssertionError("must not archive outside session"))
    monkeypatch.setattr(service.store, "save_observation", save)
    service.run_once()
    assert service.health["status"] == "idle"
    assert service.store.list_signals() == []
    assert service.notes == []


def test_accept_rejects_outside_session_before_collect(service):
    signal = triggered(service)
    service.market.now = as_cn("2026-10-16T11:30:00+08:00")
    service.market.collect = Mock(side_effect=AssertionError("must not collect"))
    with pytest.raises(LedgerError, match="交易"):
        accept(service, signal)
    assert service.store.list_plans() == []


def test_plan_evaluation_crossing_lunch_clears_ready_state(service, monkeypatch):
    account = service.store.list_accounts()[0]
    service.store.update_config(account["id"], {"max_leg_minutes": 180, "position_max_age_seconds": 7200}, now=service.market.now)
    plan = first_fill(service)
    service.market.now = as_cn("2026-10-16T11:29:59+08:00")
    target_quote(service)
    track = service._track_plan

    def crossing_track(*args):
        track(*args)
        assert service.store.get_plan(plan["id"])["monitor"]["state"] == "ready"
        service.market.now += timedelta(seconds=1)

    monkeypatch.setattr(service, "_track_plan", crossing_track)
    service.notes.clear()
    service.run_once()
    assert service.health["status"] == "idle"
    assert service.store.get_plan(plan["id"])["monitor"]["state"] == "paused"
    assert all(s["state"] == "expired" for s in service.store.list_signals())
    assert not service.notes


def test_existing_plan_retry_remains_idempotent_outside_session(service):
    plan, payload = accept(service, triggered(service))
    service.market.now = as_cn("2026-10-16T15:00:00+08:00")
    service.market.collect = Mock(side_effect=AssertionError("must not collect"))
    assert service.accept_plan(payload)["id"] == plan["id"]
    assert len(service.store.list_plans()) == 1


def test_accept_recheck_crossing_close_cannot_reserve_resources(service, monkeypatch):
    account = service.store.list_accounts()[0]
    service.store.update_config(account["id"], {"new_plan_cutoff": "15:00", "position_max_age_seconds": 20000}, now=service.market.now)
    now = service.market.now = as_cn("2026-10-16T14:56:59+08:00")
    original = sample_context()[0]
    stamps = _minute_ends(now.date(), now)
    offset = len(stamps) - len(original)
    service.market.data["bars"] = [
        {**original[max(0, i - offset)], "end": stamp.isoformat(), "datetime": stamp.isoformat()}
        for i, stamp in enumerate(stamps)
    ]
    service.market.data["quote"].update(market_ts=now, received_ts=now, book_market_ts=now)
    service.market.data["environment"].update(market_ts=now, sector_ts=now)
    signal = triggered(service)
    run = service.run_once

    def crossing_run():
        run()
        service.market.now += timedelta(seconds=1)

    monkeypatch.setattr(service, "run_once", crossing_run)
    with pytest.raises(LedgerError) as error:
        accept(service, signal)
    assert error.value.code == "outside_session"
    assert service.store.list_plans() == []
    assert service.store.get_account(account["id"])["reserved_cash"] == 0
    assert service.store.list_positions()[0]["reserved_qty"] == 0


def test_pending_opportunity_is_not_delivered_across_session_end(service):
    service.market.now = as_cn("2026-10-16T11:29:59+08:00")
    with service.store._tx() as db:
        service.store._notify(db, "late-ready", {"kind": "second_leg_ready", "plan_id": "unused",
            "expires_at": "2026-10-16T11:30:09+08:00"}, service.market.now)
    service.market.now += timedelta(seconds=1)
    service._drain_notifications(service.market.now)
    assert not service.notes
    assert not service.store.pending_notifications()
