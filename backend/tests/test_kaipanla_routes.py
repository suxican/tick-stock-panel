from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom import kaipanla
from app.market_time import CN_TZ
from app.services.kaipanla_intraday import KaipanlaUnavailableError

NOW = datetime(2026, 11, 17, 10, 30, tzinfo=CN_TZ)


@pytest.fixture
def env(monkeypatch, tmp_path):
    prefs = {"kaipanla_enabled": True}
    monkeypatch.setattr(kaipanla.preferences, "load", lambda: dict(prefs))
    saved = Mock(side_effect=lambda updates: prefs.update(updates) or dict(prefs))
    monkeypatch.setattr(kaipanla.preferences, "save", saved)
    monkeypatch.setattr(kaipanla, "cn_now", lambda: NOW)
    monkeypatch.setattr(kaipanla, "is_trading_day", lambda _now=None: True)
    live = Mock(return_value={"date": "2026-11-17", "stale_seconds": 0})
    day = Mock(return_value={"date": "2026-11-16"})
    cap = Mock()
    monkeypatch.setattr(kaipanla, "read_intraday", live)
    monkeypatch.setattr(kaipanla, "read_day", day)
    monkeypatch.setattr(kaipanla, "capture", cap)
    monkeypatch.setattr(kaipanla, "status", lambda _data_dir: {"last_success_at": None, "last_error": "x"})
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.scheduler = Mock()
    app.state.scheduler.get_job.return_value = object()
    app.include_router(kaipanla.router)
    return SimpleNamespace(app=app, prefs=prefs, saved=saved, live=live, day=day, cap=cap, data_dir=tmp_path)


def test_disabled_returns_409_and_scheduler_does_not_request(env):
    env.prefs["kaipanla_enabled"] = False
    with TestClient(env.app) as client:
        r = client.get("/api/kaipanla/intraday")
        assert r.status_code == 409 and r.json()["detail"] == "开盘啦数据未启用"
        job = env.app.state.scheduler.add_job.call_args.args[0]
        job(env.data_dir)
    env.cap.assert_not_called()
    env.live.assert_not_called()


def test_scheduler_job_runs_capture_when_enabled(env):
    with TestClient(env.app):
        job = env.app.state.scheduler.add_job.call_args.args[0]
        job(env.data_dir)
    env.cap.assert_called_once_with(env.data_dir, NOW)


def test_scheduler_skips_closed_market(env, monkeypatch):
    monkeypatch.setattr(kaipanla, "is_trading_day", lambda _now=None: False)
    with TestClient(env.app):
        env.app.state.scheduler.add_job.call_args.args[0](env.data_dir)
    env.cap.assert_not_called()


def test_today_uses_live_snapshot_and_history_reads_archive(env):
    with TestClient(env.app) as client:
        assert client.get("/api/kaipanla/intraday").json()["date"] == "2026-11-17"
        env.live.assert_called_once()
        assert client.get("/api/kaipanla/intraday", params={"date": "2026-11-17"}).is_success
        assert env.live.call_count == 2
        assert client.get("/api/kaipanla/intraday", params={"date": "2026-11-16"}).json()["date"] == "2026-11-16"
        env.day.assert_called_once_with(env.data_dir, date(2026, 11, 16))
    assert env.live.call_count == 2


def test_unavailable_maps_to_503_and_missing_archive_to_404(env):
    env.live.side_effect = KaipanlaUnavailableError("开盘啦暂不可用：超时")
    env.day.return_value = None
    with TestClient(env.app) as client:
        r = client.get("/api/kaipanla/intraday")
        assert r.status_code == 503 and r.json()["detail"] == "开盘啦暂不可用：超时"
        assert client.get("/api/kaipanla/intraday", params={"date": "2026-11-13"}).status_code == 404


def test_future_date_is_422(env):
    with TestClient(env.app) as client:
        assert client.get("/api/kaipanla/intraday", params={"date": "2026-11-18"}).status_code == 422
    env.live.assert_not_called()


def test_toggle_persists_preference(env):
    with TestClient(env.app) as client:
        assert client.put("/api/kaipanla/enabled", json={"enabled": False}).json() == {"enabled": False}
        env.saved.assert_called_once_with({"kaipanla_enabled": False})
        assert client.get("/api/kaipanla/status").json() == {"enabled": False, "last_success_at": None, "last_error": "x"}


def test_scheduler_job_registered_and_removed(env):
    with TestClient(env.app):
        call = env.app.state.scheduler.add_job.call_args
        assert call.args[1] == "cron"
        assert call.kwargs["hour"] == "9-15,21" and call.kwargs["minute"] == "*/5"
        assert call.kwargs["timezone"] == "Asia/Shanghai" and call.kwargs["id"] == "kaipanla_capture"
    env.app.state.scheduler.remove_job.assert_called_once_with("kaipanla_capture")


def test_setup_includes_router():
    registrar = Mock()
    kaipanla.setup(registrar)
    registrar.include_router.assert_called_once_with(kaipanla.router)
    assert kaipanla.EXTENSION_ID == "kaipanla.intraday"
