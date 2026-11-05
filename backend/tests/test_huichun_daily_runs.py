from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.market_time import CN_TZ
from app.services import huichun_mode
from app.services.huichun_mode import HuichunModeService

DAY = date(2026, 11, 5)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    clock = [datetime(2026, 11, 5, 16, tzinfo=CN_TZ)]
    monkeypatch.setattr(huichun_mode, "_now", lambda: clock[0])
    svc = HuichunModeService(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), data_dir=tmp_path)
    yield svc, clock, tmp_path
    svc.close()


def scan(day, *, count=0, rev=0, quality="ready", start=None):
    return {"id": f"scan-{day}-{count}", "rule_revision": rev, "start_date": str(start or day),
            "end_date": str(day), "observation_date": str(day), "observations": [],
            "candidates": [{"id": str(i), "signal_date": str(day)} for i in range(count)],
            "coverage": {"publication_status": quality, "symbol_count": 100}}


def publish(svc, data, clock):
    from app.services.huichun_daily_runs import record_scan

    snapshot = svc._snapshot.copy()
    record_scan(snapshot, data, [], clock[0], svc._calendar_path, svc.get_config()["revision"],
                huichun_mode._digest(svc._input_signature(DAY)))
    svc._write("snapshot.json", snapshot)
    svc._snapshot = snapshot


def test_waiting_missed_and_zero_completed_are_distinct_and_durable(ledger):
    svc, clock, root = ledger
    assert svc.get_daily_runs(20)["records"][0]["status"] == "waiting"
    clock[0] += timedelta(days=1)
    rows = svc.get_daily_runs(20)["records"]
    assert [r["status"] for r in rows] == ["waiting", "missed"]
    assert rows[1]["confirmed_count"] is None
    publish(svc, scan(clock[0].date(), start=DAY), clock)
    clock[0] += timedelta(days=2)
    restart = HuichunModeService(object(), data_dir=root)
    try:
        rows = restart.get_daily_runs(20)["records"]
        assert [r["date"] for r in rows] == ["2026-11-06", "2026-11-05"]
        assert rows[0]["status"] == "completed" and rows[0]["confirmed_count"] == 0
        assert rows[1]["status"] == "missed" and rows[1]["backfilled_at"] is not None
    finally:
        restart.close()


def test_same_rule_repeated_runs_revisions_and_incomplete_are_separate(ledger):
    svc, clock, _ = ledger
    first = scan(DAY)
    publish(svc, first, clock)
    clock[0] += timedelta(minutes=1)
    publish(svc, first, clock)
    publish(svc, scan(DAY, count=1), clock)
    publish(svc, scan(DAY, quality="incomplete"), clock)
    row = svc.get_daily_runs(1)["records"][0]
    assert row["confirmed_count"] == 1 and row["correction_count"] == 1
    assert row["first_completed_at"] < row["completed_at"]
    svc.save_config({"ma_window": 5}, expected_revision=0)
    row = svc.get_daily_runs(1)["records"][0]
    assert row["status"] == "waiting" and row["completed_revisions"] == [0]
    assert svc.try_auto_archive(DAY - timedelta(days=1)) == "skip"
    publish(svc, scan(DAY, rev=1), clock)
    assert svc.try_auto_archive(DAY) == "done"
    assert svc.get_daily_runs(1)["records"][0]["completed_revisions"] == [0, 1]


def test_older_archive_with_no_trigger_keeps_its_counts_and_unknown_origin(ledger):
    svc, clock, _ = ledger
    publish(svc, scan(DAY), clock)
    complete = svc._snapshot["daily_runs"]["days"][str(DAY)]["completed"]["0"]
    del complete["trigger"]
    row = svc.get_daily_runs(1)["records"][0]
    assert row["status"] == "completed" and row["confirmed_count"] == 0
    assert row["trigger"] is None


def test_restarting_interrupted_auto_job_persists_reason_and_a_weekend_is_not_missed(ledger):
    svc, clock, root = ledger
    broken = {**svc._snapshot, "job": {"kind": "scan", "status": "running", "target_date": str(DAY),
                                      "rule_revision": 0, "trigger": "daily_sync"}}
    svc._write("snapshot.json", broken)
    clock[0] += timedelta(days=2)
    restarted = HuichunModeService(svc.repo, data_dir=root)
    try:
        rows = restarted.get_daily_runs(20)["records"]
        assert [r["date"] for r in rows] == ["2026-11-06", "2026-11-05"]
        assert rows[-1]["status"] == "missed" and "重启" in rows[-1]["reason"]
        assert restarted._read("snapshot.json", {})["job"]["status"] == "failed"
    finally:
        restarted.close()


def test_waiting_preflight_is_persisted_and_missing_calendar_never_invents_dates(ledger, monkeypatch):
    svc, _, _ = ledger
    monkeypatch.setattr(svc, "daily_preflight", lambda day: {"publication_status": "incomplete",
                                                            "reason": "日线未核准"})
    assert svc.try_auto_archive(DAY) == "waiting"
    assert svc.get_daily_runs(1)["records"][0]["reason"] == "日线未核准"
    svc._calendar_path = None
    result = svc.get_daily_runs(1)
    assert result["calendar_status"] == "unavailable"
    assert [(r["date"], r["status"], r["reason"]) for r in result["records"]] == [
        (str(DAY), "waiting", "日线未核准"),
    ]


def test_real_inputs_rechecked_after_repair_concurrent_calls_and_source_only_rewrite(tmp_path, monkeypatch):
    import threading

    import polars as pl
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.huichun import router
    from app.tickflow.repository import DataStore, KlineRepository

    monkeypatch.setattr(huichun_mode, "_now", lambda: datetime(2026, 11, 5, 16, tzinfo=CN_TZ))
    store = DataStore(tmp_path / "data")
    path = store.data_dir / "kline_daily/test.parquet"
    raw = pl.DataFrame({"symbol": ["600001.SH", "600001.SH"],
                        "date": [DAY - timedelta(days=1), DAY], "open": 10., "high": 11.,
                        "low": 9., "close": 10., "volume": 1000., "amount": 10000.,
                        "daily_batch_finalized": True})
    raw.write_parquet(path)
    store._register_views()
    app = FastAPI()
    app.state.datastore = store
    app.state.repo = KlineRepository(store)
    app.include_router(router)
    try:
        with TestClient(app) as client:
            svc = app.state.huichun_mode_service
            assert client.get("/api/huichun/daily-runs?limit=0").status_code == 422
            original = svc._scan
            gate = threading.Event()
            runs = []
            def wait_scan(*args):
                runs.append(args[0])
                assert gate.wait(timeout=10)
                return original(*args)
            monkeypatch.setattr(svc, "_scan", wait_scan)
            assert svc.try_auto_archive(DAY) == "retry"
            assert svc.try_auto_archive(DAY) == "retry"
            gate.set()
            assert svc.wait()["job"]["status"] == "completed"
            assert len(runs) == 1 and svc.try_auto_archive(DAY) == "done"
            row = client.get("/api/huichun/daily-runs?limit=1").json()["records"][0]
            assert row["status"] == "completed" and row["confirmed_count"] == 0
            assert row["trigger"] == "daily_sync" and row["scan_id"] == runs[0]
            raw.with_columns(pl.lit(False).alias("daily_batch_finalized")).write_parquet(path)
            assert svc.try_auto_archive(DAY) == "waiting"
            assert svc.get_daily_runs(1)["records"][0]["status"] == "waiting"
            raw.write_parquet(path)
            assert svc.try_auto_archive(DAY) == "retry"
            assert svc.wait()["job"]["status"] == "completed"
            assert svc.try_auto_archive(DAY) == "done"
            assert len(runs) == 2
            assert svc.get_daily_runs(1)["records"][0]["correction_count"] == 0
    finally:
        store.db.close()
