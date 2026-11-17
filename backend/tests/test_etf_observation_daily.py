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


def test_overdue_only_when_a_session_before_today_is_missing(tmp_path):
    def published(name, *days):
        for d in days:
            (tmp_path / name / "ext_data" / TABLE / "timeseries" / f"date={d}").mkdir(parents=True)
        return tmp_path / name

    assert not daily.overdue(published("empty"), date(2026, 11, 16))  # no audited release yet
    current = published("current", "2026-11-04", "2026-11-13")
    assert not daily.overdue(current, date(2026, 11, 16))  # Monday: Friday is the latest due
    assert daily.overdue(current, date(2026, 11, 17))  # Monday's session is now missing
    assert daily.overdue(published("behind", "2026-11-04", "2026-11-10"), date(2026, 11, 16))


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


def test_startup_runs_a_missed_slot_once_and_skips_when_current(release, monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.custom import etf_observation
    from app.market_time import CN_TZ

    jobs = {}
    scheduler = SimpleNamespace(
        add_job=lambda func, trigger, **kw: jobs.update({kw["id"]: (func, trigger, kw)}),
        get_job=lambda job_id: jobs.get(job_id),
        remove_job=lambda job_id: jobs.pop(job_id),
    )
    now = datetime(2026, 11, 16, 15, 0, tzinfo=CN_TZ)
    appended = []
    monkeypatch.setattr(etf_observation, "cn_now", lambda: now)
    monkeypatch.setattr(etf_observation, "append_new_sessions", lambda d, today: appended.append(today) or [])
    root = release("2026-11-04", "2026-11-10")
    app = FastAPI()
    app.state.scheduler = scheduler
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=root))
    app.include_router(etf_observation.router)
    with TestClient(app):
        func, trigger, kw = jobs[etf_observation._CATCH_UP_JOB]
        assert trigger == "date" and kw["run_date"] > now
        func(*kw["args"])  # backend was down for the 8:30/21:30 slots since 11-10
        assert appended == [date(2026, 11, 16)]
        release("2026-11-13")
        func(*kw["args"])  # caught up: no exchange requests on every restart
        assert appended == [date(2026, 11, 16)]
    assert jobs == {}


def test_catch_up_and_cron_never_write_the_release_concurrently(tmp_path, monkeypatch):
    from app.custom import etf_observation

    calls = []
    monkeypatch.setattr(etf_observation, "append_new_sessions", lambda d, today: calls.append(today) or [])
    with etf_observation._APPEND_LOCK:  # the other job is mid-run
        etf_observation._append(tmp_path)
    assert calls == []
    etf_observation._append(tmp_path)
    assert len(calls) == 1 and not etf_observation._APPEND_LOCK.locked()


@pytest.fixture
def manual(release, monkeypatch):
    """Data page button: POST /ledger/append on a release published through Friday 11-13."""
    from datetime import datetime
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.custom import etf_observation
    from app.market_time import CN_TZ

    root = release("2026-11-04", "2026-11-13")
    monkeypatch.setattr(etf_observation, "cn_now", lambda: datetime(2026, 11, 17, 8, 30, tzinfo=CN_TZ))
    app = FastAPI()
    app.include_router(etf_observation.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=root))

    def post(append):
        monkeypatch.setattr(etf_observation, "append_new_sessions", append)
        return TestClient(app).post("/api/etf-observation/ledger/append")
    return post, release


def test_manual_update_reports_added_and_still_pending_sessions(manual):
    post, release = manual

    def append(data_dir, today):
        assert today == date(2026, 11, 17)
        release("2026-11-16")
        return ["2026-11-16"]

    response = post(append)
    assert response.status_code == 200
    assert response.json() == {"added": ["2026-11-16"], "latest": "2026-11-16", "pending": ["2026-11-17"]}


def test_manual_update_without_new_sessions_names_what_is_still_missing(manual):
    post, _ = manual
    response = post(lambda data_dir, today: [])
    assert response.status_code == 200
    assert response.json() == {"added": [], "latest": "2026-11-13", "pending": ["2026-11-16", "2026-11-17"]}


def test_manual_update_explains_a_source_refusal_without_leaking_the_query(manual):
    import httpx

    post, _ = manual
    url = "http://eid.csrc.gov.cn/fund/disclose/getPublicFundJZInfoMore.do?aoData=secret"

    def refused(data_dir, today):
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError("405", request=request, response=httpx.Response(405, request=request))

    response = post(refused)
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "eid.csrc.gov.cn" in detail and "405" in detail and "aoData" not in detail


def test_manual_update_reports_network_failures_and_internal_errors_in_plain_words(manual, caplog):
    import httpx

    post, _ = manual

    def offline(data_dir, today):
        raise httpx.ConnectError("boom", request=httpx.Request("GET", "https://query.sse.com.cn/x"))

    response = post(offline)
    assert response.status_code == 502 and "query.sse.com.cn" in response.json()["detail"]

    def conflict(data_dir, today):
        raise ValueError("Published snapshot differs on 2026-11-13; review the revision")

    response = post(conflict)
    assert response.status_code == 500
    assert "Published" not in response.json()["detail"] and "snapshot differs" in caplog.text


def test_manual_update_waits_for_a_scheduled_run_in_progress(manual):
    from app.custom import etf_observation

    post, _ = manual
    calls = []
    with etf_observation._APPEND_LOCK:
        response = post(lambda data_dir, today: calls.append(today) or [])
    assert response.status_code == 409 and calls == []
