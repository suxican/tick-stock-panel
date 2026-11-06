import asyncio
import threading
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timedelta
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.market_time import CN_TZ
from app.services.huichun_schedule import HuichunArchiveSchedule, completed_sync_day

DAY = date(2026, 11, 5)
NOW = datetime(2026, 11, 5, 16, tzinfo=CN_TZ)


def job(**changes):
    return {
        "id": "sync-one", "status": "succeeded", "finished_at": "2026-11-05T07:35:00Z",
        "started_at": "2026-11-05T07:32:00Z", "stage": "done", "progress": 100,
        "duration_s": 180, "error": None,
        "result": {"stock_daily_sync_date": str(DAY), "stock_daily_written_rows": 0},
        **changes,
    }


@pytest.mark.parametrize("changes", [
    {"status": "failed"}, {"status": "running"}, {"finished_at": "2026-11-05T02:50:00Z"},
    {"finished_at": "2026-11-04T08:00:00Z"}, {"finished_at": None}, {"finished_at": "2026-11-05T15:35:00"},
    {"result": {"daily_days": 1}}, {"result": {"stock_daily_sync_date": None}},
    {"result": {"stock_daily_sync_date": "2026-11-04"}}, {"result": None},
])
def test_only_an_explicit_current_after_close_stock_sync_receipt_wakes_archive(changes):
    assert completed_sync_day(job(**changes), NOW) is None
    assert completed_sync_day(job(), NOW) == DAY


def test_waiting_result_actually_retries_data_then_archives(monkeypatch):
    from app.services import huichun_schedule as module

    monkeypatch.setattr(module, "cn_now", lambda: NOW)
    archived = threading.Event()
    service = Mock()
    service.try_auto_archive.side_effect = ["waiting", "retry", "done"]
    schedule = HuichunArchiveSchedule(service, repair=Mock(), retry_delays=(0, 0))
    done = service.try_auto_archive.side_effect

    def attempt(day):
        assert day == DAY
        outcome = next(done)
        if outcome == "done":
            archived.set()
        return outcome

    service.try_auto_archive.side_effect = attempt
    try:
        schedule.resume([job(status="failed"), job()])
        assert archived.wait(3)
        schedule.offer(job())
        assert service.try_auto_archive.call_count == 3
        schedule.repair.assert_called_once()
        assert schedule.repair.call_args.args[0] == DAY
        assert callable(schedule.repair.call_args.args[1])
    finally:
        schedule.close()


def test_schedule_coalesces_same_job_and_can_stop_a_retry(monkeypatch):
    from app.services import huichun_schedule as module

    monkeypatch.setattr(module, "cn_now", lambda: NOW)
    called = threading.Event()
    service = SimpleNamespace(try_auto_archive=lambda day: called.set() or "waiting")
    repair = Mock()
    schedule = HuichunArchiveSchedule(service, repair=repair, retry_delays=(600,))
    schedule.offer(job())
    assert called.wait(3)
    schedule.offer(job())
    schedule.close()
    schedule.offer(job(id="later"))
    repair.assert_not_called()


def retry_clock(monkeypatch, schedule, *, start=NOW, interrupt=None):
    from app.services import huichun_schedule as module

    clock = [start]
    monkeypatch.setattr(module, "cn_now", lambda: clock[0])

    def wait_for(predicate, timeout):
        if timeout > 600 and interrupt:
            interrupt(schedule, clock)
        else:
            clock[0] += timedelta(seconds=timeout)
        return predicate()

    monkeypatch.setattr(schedule._condition, "wait_for", wait_for)
    return clock


def test_evening_recheck_repairs_and_archives_after_short_retries_exhausted(monkeypatch, caplog):
    service, repair = Mock(), Mock()
    repair.side_effect = ["busy", "busy", "busy", None]
    schedule = HuichunArchiveSchedule(service, repair=repair)
    clock = retry_clock(monkeypatch, schedule)
    attempted = []

    def archive(day):
        assert day == DAY
        attempted.append(clock[0])
        return "done" if clock[0].hour >= 18 else "waiting"

    service.try_auto_archive.side_effect = archive
    with caplog.at_level("INFO"):
        schedule._attempts(DAY)
    assert [stamp.strftime("%H:%M") for stamp in attempted] == ["16:00", "16:01", "16:04", "16:14", "18:00"]
    assert repair.call_count == 4
    assert all(call.args[0] == DAY for call in repair.call_args_list)
    assert sum("核准通道占用" in record.message for record in caplog.records) == 3
    service.record_daily_waiting.assert_not_called()


@pytest.mark.parametrize("start,expected", [(NOW, 6), (NOW.replace(hour=21), 4)])
def test_evening_rechecks_are_bounded_and_do_not_replay_past_times(monkeypatch, start, expected):
    service, repair = Mock(), Mock()
    service.try_auto_archive.return_value = "waiting"
    schedule = HuichunArchiveSchedule(service, repair=repair)
    clock = retry_clock(monkeypatch, schedule, start=start)
    schedule._attempts(DAY)
    assert service.try_auto_archive.call_count == expected
    assert repair.call_count == expected - 1
    assert clock[0].date() == DAY


@pytest.mark.parametrize("interruption", ["close", "new_receipt", "next_day"])
def test_evening_wait_stops_on_shutdown_superseding_receipt_or_next_day(monkeypatch, interruption):
    service, repair = Mock(), Mock()
    service.try_auto_archive.return_value = "waiting"
    schedule = HuichunArchiveSchedule(service, repair=repair)

    def interrupt(schedule, clock):
        if interruption == "close":
            schedule._closed = True
        elif interruption == "new_receipt":
            schedule._pending = DAY
        else:
            clock[0] += timedelta(days=1)

    retry_clock(monkeypatch, schedule, interrupt=interrupt)
    schedule._attempts(DAY)
    assert service.try_auto_archive.call_count == 4
    assert repair.call_count == 3


def test_evening_provider_failures_remain_bounded_and_report_failure(monkeypatch):
    service, repair = Mock(), Mock(side_effect=ValueError("provider unavailable"))
    service.try_auto_archive.return_value = "waiting"
    schedule = HuichunArchiveSchedule(service, repair=repair)
    retry_clock(monkeypatch, schedule)
    schedule._attempts(DAY)
    assert repair.call_count == 5
    assert service.record_daily_waiting.call_count == 5
    service.try_auto_archive.assert_called_once_with(DAY)


def test_close_interrupts_real_evening_wait_without_starting_repair(monkeypatch):
    from app.services import huichun_schedule as module

    monkeypatch.setattr(module, "cn_now", lambda: NOW)
    attempted = threading.Event()
    service, repair = Mock(), Mock()
    service.try_auto_archive.side_effect = lambda _day: attempted.set() or "waiting"
    schedule = HuichunArchiveSchedule(service, repair=repair, retry_delays=())
    try:
        schedule.offer(job())
        assert attempted.wait(3)
        schedule.close()
        assert schedule._thread is None
        repair.assert_not_called()
    finally:
        schedule.close()


@pytest.mark.parametrize("entry", ["scheduled", "manual", "other"])
@pytest.mark.parametrize("fail", [False, True])
def test_pipeline_only_offers_success_after_releasing_slot(monkeypatch, entry, fail):
    from app.api import pipeline
    from app.jobs import daily_pipeline
    from app.services import pipeline_jobs

    steps = []
    finished = threading.Event()
    store = Mock()
    store.create.return_value = ("j1", True)
    store.succeed.side_effect = lambda *_: steps.append("success")
    store.fail.side_effect = lambda *_: steps.append("failure")
    store.get.return_value = job()
    monkeypatch.setattr(daily_pipeline, "_get_app_state", lambda: SimpleNamespace(
        huichun_archive_schedule=SimpleNamespace(offer=lambda *_: steps.append("offer")),
    ))

    def release(_):
        steps.append("released")
        finished.set()

    for target in (pipeline_jobs, pipeline):
        monkeypatch.setattr(target, "job_store", store)
        monkeypatch.setattr(target, "try_acquire_run_slot", lambda _: True)
        monkeypatch.setattr(target, "release_run_slot", release)
        monkeypatch.setattr(target, "run_with_capacity", lambda _jid, fn: fn())

    def perform(*_args, **_kwargs):
        if fail:
            raise ValueError("job failed")
        return job()["result"]

    if entry == "scheduled":
        assert daily_pipeline._run_tracked(perform, "daily_pipeline") is (not fail)
    else:
        async def run():
            monkeypatch.setattr(pipeline, "invalidate_storage_cache", lambda: None)
            repo = SimpleNamespace(refresh_cache=lambda: None)
            if entry == "manual":
                monkeypatch.setattr(daily_pipeline, "run_now", perform)
                await pipeline.trigger_pipeline_job(repo, object())
            else:
                await pipeline._submit_job(repo, None, lambda _progress: perform)
            # Include the post-success callback running off the event loop.
            tasks = asyncio.all_tasks() - {asyncio.current_task()}
            await asyncio.gather(*tasks)
        asyncio.run(run())
    assert finished.is_set()
    assert steps == (["failure", "released"] if fail else
                     ["success", "released", *(["offer"] if entry != "other" else [])])


def test_targeted_repair_uses_only_unconfirmed_symbols_and_releases_write_slot(monkeypatch):
    from app.services import huichun_schedule as module
    from app.services import kline_sync, pipeline_jobs
    from app.services.heavy_job_limiter import shared_heavy_job_limiter

    monkeypatch.setattr(module, "cn_now", lambda: NOW)
    qs = Mock()
    qs.paused.side_effect = nullcontext
    state = SimpleNamespace(repo=object(), capabilities=object(), quote_service=qs)
    service = Mock()
    service.daily_preflight.return_value = {"needs_symbols": ["600001.SH"]}
    def refresh_and_fail(*args, **kwargs):
        # Repository cache refresh acquires the same exclusive slot in production.
        with shared_heavy_job_limiter.slot("exclusive", timeout=0):
            raise ValueError("provider error")

    confirm = Mock(side_effect=refresh_and_fail)
    monkeypatch.setattr(kline_sync, "confirm_daily_session", confirm, raising=False)
    with pytest.raises(ValueError, match="provider error"):
        module.repair_daily_evidence(service, state, DAY)
    confirm.assert_called_once_with(state.repo, state.capabilities, DAY, ["600001.SH"], on_chunk_done=None)
    assert pipeline_jobs.try_acquire_run_slot("other-sync")
    try:
        assert module.repair_daily_evidence(service, state, DAY) == "busy"
        assert confirm.call_count == 1
    finally:
        pipeline_jobs.release_run_slot("other-sync")


def test_shutdown_during_targeted_fetch_releases_quotes_and_both_write_slots(monkeypatch):
    from app.services import huichun_schedule, kline_sync, pipeline_jobs
    from app.services.heavy_job_limiter import shared_heavy_job_limiter

    monkeypatch.setattr(huichun_schedule, "cn_now", lambda: NOW)
    fetching, resumed = threading.Event(), threading.Event()

    @contextmanager
    def pause():
        try:
            yield
        finally:
            resumed.set()

    def fetch(*_args, on_chunk_done):
        fetching.set()
        while not resumed.wait(0.01):
            on_chunk_done(1, 2)

    monkeypatch.setattr(kline_sync, "confirm_daily_session", fetch)
    state = SimpleNamespace(repo=object(), capabilities=object(), quote_service=SimpleNamespace(paused=pause))
    service = Mock()
    service.try_auto_archive.return_value = "waiting"
    service.daily_preflight.return_value = {"needs_symbols": ["600001.SH"]}
    schedule = HuichunArchiveSchedule(
        service, repair=partial(huichun_schedule.repair_daily_evidence, service, state), retry_delays=(0,),
    )
    try:
        schedule.offer(job())
        assert fetching.wait(3)
        schedule.close()
        assert resumed.is_set() and shared_heavy_job_limiter.in_use == 0
        assert pipeline_jobs.try_acquire_run_slot("other-sync")
        pipeline_jobs.release_run_slot("other-sync")
        service.try_auto_archive.assert_called_once_with(DAY)
    finally:
        resumed.set()
        schedule.close()


def test_router_restart_recovers_today_and_keeps_zero_signal_archive_idempotent(tmp_path, monkeypatch):
    import json
    import time
    from datetime import timedelta

    import polars as pl
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import huichun as api
    from app.services import huichun_mode, huichun_schedule
    from app.services.pipeline_jobs import JobStore
    from app.tickflow.repository import DataStore, KlineRepository

    monkeypatch.setattr(huichun_mode, "_now", lambda: NOW)
    monkeypatch.setattr(huichun_schedule, "cn_now", lambda: NOW)
    root = tmp_path / "data"
    jobs = JobStore(store_dir=root / "job_store")
    (jobs.directory / "sync-one.json").write_text(json.dumps(job()), encoding="utf-8")
    monkeypatch.setattr(api, "job_store", jobs)
    store = DataStore(root)
    raw = pl.DataFrame({
        "symbol": ["600001.SH"] * 2, "date": [DAY - timedelta(days=1), DAY],
        "open": 10., "high": 11., "low": 9., "close": 10., "volume": 1000.,
        "amount": 10000., "daily_batch_finalized": True,
    })
    raw.write_parquet(root / "kline_daily/test.parquet")
    store._register_views()
    app = FastAPI()
    app.state.datastore = store
    app.state.repo = KlineRepository(store)
    app.include_router(api.router)
    try:
        rows = []
        for _ in range(2):
            # No monitor rules, notification service, or browser scan requests.
            with TestClient(app) as client:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    row = client.get("/api/huichun/daily-runs?limit=1").json()["records"][0]
                    if row["status"] == "completed":
                        rows.append(row)
                        break
                    time.sleep(0.02)
        assert len(rows) == 2 and rows[0] == rows[1]
        assert rows[0]["confirmed_count"] == rows[0]["pending_count"] == 0
        assert len(list((root / "user_data/huichun").glob("scan-*.json"))) == 1
    finally:
        store.db.close()
