from datetime import date

import pytest

from app.custom.etf_observation_data import daily
from app.custom.etf_observation_data.ledger import TABLE


@pytest.fixture
def release(tmp_path):
    def make(*days):
        for d in days:
            (tmp_path / "ext_data" / TABLE / "timeseries" / f"date={d}").mkdir(parents=True)
        return tmp_path
    return make


@pytest.fixture
def calls(monkeypatch):
    log = {"shares": [], "nav": [], "build": []}
    state = {"shares_until": date(2026, 11, 30), "nav_until": date(2026, 11, 30)}

    def shares(output, start, end):
        log["shares"].append(end)
        return [] if end <= state["shares_until"] else [("510300", end.isoformat())]

    def nav(output, start, end):
        log["nav"].append(end)
        return [] if end <= state["nav_until"] else [("510300", end.isoformat())]

    def build(data_dir, end):
        log["build"].append(end)
        return [end.isoformat()]

    monkeypatch.setattr(daily, "collect_shares", shares)
    monkeypatch.setattr(daily, "collect_nav", nav)
    monkeypatch.setattr(daily, "build", build)
    return log, state


def test_appends_through_the_latest_session_with_complete_evidence(release, calls):
    log, state = calls
    root = release("2026-11-03", "2026-11-04")
    state["nav_until"] = date(2026, 11, 9)  # regulator NAVs lag by a day
    assert daily.append_new_sessions(root, date(2026, 11, 10)) == ["2026-11-09"]
    assert log["shares"] == [date(2026, 11, 10), date(2026, 11, 9)]
    assert log["nav"] == [date(2026, 11, 10), date(2026, 11, 9)]
    assert log["build"] == [date(2026, 11, 9)]


def test_waits_without_writing_when_nothing_new_is_published(release, calls):
    log, state = calls
    root = release("2026-11-04")
    state["shares_until"] = date(2026, 11, 4)
    assert daily.append_new_sessions(root, date(2026, 11, 6)) == []
    assert log["shares"] == [date(2026, 11, 6), date(2026, 11, 5)] and log["build"] == []


def test_no_network_when_up_to_date_or_without_an_audited_release(release, calls, tmp_path):
    log, _ = calls
    assert daily.append_new_sessions(tmp_path, date(2026, 11, 10)) == []  # no release yet
    root = release("2026-11-04", "2026-11-05", "2026-11-06")
    assert daily.append_new_sessions(root, date(2026, 11, 8)) == []  # weekend, nothing pending
    assert log == {"shares": [], "nav": [], "build": []}


def test_stops_at_the_end_of_the_bundled_calendar(release, calls):
    log, _ = calls
    root = release("2026-11-04")
    coverage_end = daily.read_market_calendar(daily.BUNDLED_CALENDAR_PATH).coverage_end
    daily.append_new_sessions(root, date(coverage_end.year + 1, 1, 15))
    assert log["shares"] and max(log["shares"]) <= coverage_end


def test_job_is_scheduled_with_the_app_and_contains_failures(tmp_path, monkeypatch, caplog):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.custom import etf_observation

    jobs = {}
    scheduler = SimpleNamespace(
        add_job=lambda func, trigger, **kw: jobs.update({kw["id"]: (func, kw)}),
        get_job=lambda job_id: jobs.get(job_id),
        remove_job=lambda job_id: jobs.pop(job_id),
    )
    app = FastAPI()
    app.state.scheduler = scheduler
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(etf_observation.router)
    with TestClient(app):
        func, kw = jobs[etf_observation._APPEND_JOB]
        assert kw["hour"] == "8,21" and kw["timezone"] == "Asia/Shanghai" and kw["max_instances"] == 1

        def boom(*args):
            raise OSError("exchange unreachable")

        monkeypatch.setattr(etf_observation, "append_new_sessions", boom)
        func(*kw["args"])  # must not raise into the scheduler
        assert "daily append failed" in caplog.text
    assert jobs == {}
