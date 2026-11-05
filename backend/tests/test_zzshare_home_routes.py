from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom import zzshare
from app.market_time import CN_TZ


def test_home_route_and_sampler_share_the_same_archive(monkeypatch, tmp_path):
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.scheduler = scheduler = Mock()
    app.include_router(zzshare.router)
    read = Mock(return_value={"rows": []})
    monkeypatch.setattr(zzshare, "read_home", read)
    now = datetime(2026, 11, 4, 10, 40, tzinfo=CN_TZ)
    monkeypatch.setattr(zzshare, "cn_now", lambda: now)
    monkeypatch.setattr(zzshare, "is_trading_day", lambda _: True)
    with TestClient(app) as client:
        scheduler.add_job.assert_called_once()
        sample = scheduler.add_job.call_args.args[0]
        sample(tmp_path)
        read.assert_called_once_with(tmp_path, now.date())
        read.reset_mock()
        assert client.get("/api/zzshare/home-topics", params={"date": "2026-11-04",
            "as_of": "2026-11-04T10:30:00+08:00"}).is_success
        read.assert_called_once_with(tmp_path, now.date(), as_of=now.replace(minute=30))
        read.reset_mock()
        for stamp in ("2026-11-04T10:30", "2026-11-04T12:00:00+08:00", "2026-11-03T10:30:00+08:00"):
            assert client.get("/api/zzshare/home-topics", params={"date": "2026-11-04", "as_of": stamp}).status_code == 422
        assert client.get("/api/zzshare/home-topics", params={"date": "2026-11-05"}).status_code == 422
        read.assert_not_called()
    scheduler.remove_job.assert_called_once()


def test_no_background_requests_during_lunch_or_holiday(monkeypatch, tmp_path):
    read = Mock()
    monkeypatch.setattr(zzshare, "read_home", read)
    for hour, open_day in ((10, False), (12, True)):
        monkeypatch.setattr(zzshare, "is_trading_day", lambda _, open_day=open_day: open_day)
        monkeypatch.setattr(zzshare, "cn_now", lambda hour=hour: datetime(2026, 11, 4, hour, tzinfo=CN_TZ))
        zzshare._capture_home(tmp_path)
    read.assert_not_called()
