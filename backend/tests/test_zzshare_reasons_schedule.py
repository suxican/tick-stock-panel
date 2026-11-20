from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.custom import zzshare as extension
from app.extensions.registry import BackendExtensionRegistrar
from app.market_time import CN_TZ
from app.services import preferences

URL = "/api/zzshare/limit-reasons/schedule"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    preferences._invalidate_cache()
    app = FastAPI()
    registrar = BackendExtensionRegistrar(extension.EXTENSION_ID, api_version=1)
    extension.setup(registrar)
    app.include_router(registrar.routers[0])
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path, db=Mock()))
    app.state.scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    app.state.scheduler.start(paused=True)
    yield SimpleNamespace(app=app, client=TestClient(app))
    app.state.scheduler.shutdown(wait=False)
    preferences._invalidate_cache()


def test_default_is_disabled_with_1730(env):
    body = env.client.get(URL).json()
    assert body["enabled"] is False and body["time"] == "17:30" and body["next_run"] is None
    assert env.app.state.scheduler.get_job(extension._REASONS_JOB) is None


def test_enable_registers_cron_job_and_disable_removes_it(env):
    body = env.client.put(URL, json={"enabled": True, "time": "16:45"}).json()
    job = env.app.state.scheduler.get_job(extension._REASONS_JOB)
    assert job is not None and body["enabled"] and body["time"] == "16:45" and body["next_run"]
    assert env.client.get(URL).json()["time"] == "16:45"
    # 改时间: 同一任务被替换而不是重复注册
    env.client.put(URL, json={"enabled": True, "time": "18:00"})
    assert len(env.app.state.scheduler.get_jobs()) == 1
    env.client.put(URL, json={"enabled": False, "time": "18:00"})
    assert env.app.state.scheduler.get_job(extension._REASONS_JOB) is None


@pytest.mark.parametrize("bad", ["24:00", "9:30", "17:60", "abc"])
def test_rejects_invalid_time(env, bad):
    assert env.client.put(URL, json={"enabled": True, "time": bad}).status_code == 422


def _run(env, monkeypatch, now, sync):
    monkeypatch.setattr(extension, "cn_now", lambda: now)
    monkeypatch.setattr(extension, "sync_limit_reasons", sync)
    monkeypatch.setattr(extension, "_refresh_views", Mock())
    extension._run_reasons_sync(env.app)


def test_run_skips_non_trading_day(env, monkeypatch):
    sync = Mock()
    monkeypatch.setattr(extension, "is_trading_day", lambda _: False)
    _run(env, monkeypatch, datetime(2026, 11, 21, 17, 30, tzinfo=CN_TZ), sync)
    sync.assert_not_called()
    assert preferences.load().get("zzshare_reasons_last_run") is None


def test_run_records_synced_and_errors(env, monkeypatch):
    now = datetime(2026, 11, 20, 17, 30, tzinfo=CN_TZ)
    monkeypatch.setattr(extension, "is_trading_day", lambda _: True)
    _run(env, monkeypatch, now, Mock(return_value={"state": "synced"}))
    assert env.client.get(URL).json()["last_run"]["state"] == "synced"
    _run(env, monkeypatch, now, Mock(side_effect=RuntimeError("boom")))
    last = env.client.get(URL).json()["last_run"]
    assert last["state"] == "error" and last["message"] == "boom" and last["date"] == "2026-11-20"
