"""交易所参考数据: 猫爪取数口径、按日同步落盘、回填与调度门禁。"""
from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom import market_reference as ext
from app.data_providers.schemas import LIMIT_EVENT_SCHEMA, PRICE_LIMIT_SCHEMA, ST_STATUS_SCHEMA
from app.market_time import CN_TZ
from app.plugins.meoz.client import MeozError
from app.plugins.meoz.provider import MeozProvider
from app.services import market_reference as svc

DAY = date(2026, 11, 18)
EVENING = datetime(2026, 11, 18, 16, 0, tzinfo=CN_TZ)


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def query(self, api, params=None, fields=None, service="live"):
        self.calls.append((api, dict(params or {})))
        return self.responses[api]


def _provider(responses) -> tuple[MeozProvider, FakeClient]:
    provider = MeozProvider()
    provider._client = fake = FakeClient({"stockbasic": [{"code": "000001.SZ", "symbol": "000001"}], **responses})
    return provider, fake


# ---- 猫爪取数口径 ----

def test_price_limits_map_sentinel_and_infer_delisted_suffix():
    provider, fake = _provider({"pricelimit": [
        {"symbol": "000001", "tradedate": "20261118", "pre_close": 12.92, "up_limit": 13.48, "down_limit": 11.03},
        {"symbol": "920309", "tradedate": "20261118", "pre_close": 31.92, "up_limit": 99999.999, "down_limit": 0.01},
        {"symbol": "600087", "tradedate": "20261118", "pre_close": 1, "up_limit": 1.1, "down_limit": 0.9},
        {"symbol": "900901", "tradedate": "20261118", "pre_close": 1, "up_limit": 1.1, "down_limit": 0.9},
    ]})
    frame = provider.get_price_limits(DAY)
    assert frame.schema == pl.Schema(PRICE_LIMIT_SCHEMA)
    assert frame.to_dicts() == [
        {"date": DAY, "symbol": "000001.SZ", "limit_up": 13.48, "limit_down": 11.03, "no_limit": False, "source": "meoz"},
        {"date": DAY, "symbol": "920309.BJ", "limit_up": None, "limit_down": None, "no_limit": True, "source": "meoz"},
        {"date": DAY, "symbol": "600087.SH", "limit_up": 1.1, "limit_down": 0.9, "no_limit": False, "source": "meoz"},
    ]  # B 股代码段无法识别被丢弃; pre_close (历史为前复权) 不落盘
    assert ("pricelimit", {"tradedate": "20261118"}) in fake.calls


def test_st_status_queries_by_date_range():
    provider, fake = _provider({"st": [
        {"symbol": "000001", "name": "*ST甲", "tradedate": "20261118", "type": "ST"},
        {"symbol": "000001", "name": "*ST甲", "tradedate": "20261118", "type": "ST"},
    ]})
    frame = provider.get_st_status(DAY)
    assert frame.schema == pl.Schema(ST_STATUS_SCHEMA) and frame.height == 1
    assert ("st", {"startdate": "20261118", "enddate": "20261118"}) in fake.calls


def test_limit_events_convert_percent_and_keep_timeline():
    detail = [{"type": "u", "time": "09:33:18"}, {"type": "ub", "time": "09:36:27"}]
    provider, _ = _provider({"limit_pool": [
        {"symbol": "000001", "name": "甲", "tradedate": "20261118", "type": "u", "is_break": False,
         "limit_times": 2, "pre_limit_times": 1, "first_time": "09:33:18", "last_time": "09:47:09",
         "open_times": 1, "fd_amount": 5772865, "limit_detail": detail, "close": 0.95, "pct_chg": 10.47,
         "amount": 57784020},
    ]})
    frame = provider.get_limit_events(DAY)
    assert frame.schema == pl.Schema(LIMIT_EVENT_SCHEMA)
    row = frame.row(0, named=True)
    assert row["change_pct"] == pytest.approx(0.1047)
    assert json.loads(row["limit_detail"]) == detail
    assert (row["limit_times"], row["open_times"], row["fd_amount"]) == (2, 1, 5772865.0)


@pytest.mark.parametrize("rows, message", [([], "无数据"), ([{"symbol": "000001", "tradedate": "20261117"}], "尚未更新")])
def test_empty_or_stale_upstream_is_rejected(rows, message):
    provider, _ = _provider({"pricelimit": rows})
    with pytest.raises(MeozError, match=message):
        provider.get_price_limits(DAY)


# ---- 同步落盘 ----

def _limits(day=DAY, symbols=("000001.SZ",)) -> pl.DataFrame:
    return pl.DataFrame([{"date": day, "symbol": s, "limit_up": 11.0, "limit_down": 9.0, "no_limit": False,
                          "source": "meoz"} for s in symbols], schema=PRICE_LIMIT_SCHEMA)


@pytest.fixture
def env(monkeypatch, tmp_path):
    fetch = Mock(side_effect=lambda day: _limits(day))
    provider = SimpleNamespace(get_price_limits=fetch, get_st_status=Mock(), get_limit_events=Mock())
    routes = [{"id": ds, "source": "meoz", "source_display": "猫爪数据", "usable": True} for ds in svc.DATASETS]
    monkeypatch.setattr(svc, "resolve_capability_sources", lambda _tier: routes)
    monkeypatch.setattr(svc.custom_sources, "get_provider", lambda name: provider)
    monkeypatch.setattr(svc, "cn_now", lambda: EVENING)
    for day in ("2019-12-31", "2020-01-02", "2026-11-17", "2026-11-18"):
        (tmp_path / "kline_daily" / f"date={day}").mkdir(parents=True)
    with svc._backfill_lock:
        svc._backfill.clear()
    return SimpleNamespace(fetch=fetch, routes=routes, provider=provider, data_dir=tmp_path)


def test_sync_writes_once(env):
    assert svc.sync_day(env.data_dir, "price_limits", DAY) == {"state": "saved", "rows": 1, "source": "meoz"}
    saved = pl.read_parquet(svc.path_of(env.data_dir, "price_limits", DAY))
    assert saved.equals(_limits())
    assert svc.sync_day(env.data_dir, "price_limits", DAY) == {"state": "cached"}
    assert env.fetch.call_count == 1


@pytest.mark.parametrize("bad", [
    pl.DataFrame(schema=PRICE_LIMIT_SCHEMA),
    _limits(day=date(2026, 11, 17)),
    _limits(symbols=("000001.SZ", "000001.SZ")),
    _limits().drop("no_limit"),
])
def test_invalid_frames_are_not_written(env, bad):
    env.fetch.side_effect = lambda day: bad
    with pytest.raises(svc.ReferenceDataError):
        svc.sync_day(env.data_dir, "price_limits", DAY)
    assert not svc.path_of(env.data_dir, "price_limits", DAY).exists()


def test_today_waits_for_ready_time_and_future_rejected(env, monkeypatch):
    monkeypatch.setattr(svc, "cn_now", lambda: datetime(2026, 11, 18, 14, 0, tzinfo=CN_TZ))
    with pytest.raises(ValueError, match="15:30"):
        svc.sync_day(env.data_dir, "limit_events", DAY)
    with pytest.raises(ValueError):
        svc.sync_day(env.data_dir, "price_limits", date(2026, 11, 19))
    assert svc.sync_day(env.data_dir, "price_limits", DAY)["state"] == "saved"  # 9:00 后即可


def test_unusable_route_does_not_fetch(env):
    env.routes[0]["usable"] = False
    with pytest.raises(svc.ReferenceUnavailableError):
        svc.sync_day(env.data_dir, "price_limits", DAY)
    env.fetch.assert_not_called()


def test_backfill_uses_local_days_from_upstream_start_and_skips_existing(env):
    svc.sync_day(env.data_dir, "price_limits", date(2026, 11, 17))
    env.fetch.reset_mock()
    seen = []
    result = svc.backfill(env.data_dir, "price_limits", on_progress=seen.append)
    assert [c.args[0] for c in env.fetch.call_args_list] == [date(2020, 1, 2), DAY]  # 2019 早于上游起点
    assert (result["state"], result["total"], result["todo"], result["saved"]) == ("succeeded", 3, 2, 2)
    assert seen[-1]["done"] == 2


def test_backfill_excludes_today_before_ready(env, monkeypatch):
    monkeypatch.setattr(svc, "cn_now", lambda: datetime(2026, 11, 18, 8, 0, tzinfo=CN_TZ))
    svc.backfill(env.data_dir, "price_limits")
    assert DAY not in [c.args[0] for c in env.fetch.call_args_list]


def test_backfill_stops_after_consecutive_errors(env, monkeypatch):
    days = [date(2021, 1, d) for d in range(4, 12)]
    monkeypatch.setattr(svc, "local_trading_days", lambda *_a: days)
    env.fetch.side_effect = MeozError("HTTP 503")
    result = svc.backfill(env.data_dir, "price_limits")
    assert result["state"] == "failed" and "连续 5 天" in result["stopped_reason"]
    assert env.fetch.call_count == svc.MAX_CONSECUTIVE_ERRORS and len(result["errors"]) == 5


def test_backfill_stop_and_unavailable(env):
    assert svc.backfill(env.data_dir, "price_limits", should_stop=lambda: True)["state"] == "stopped"
    env.routes[0]["usable"] = False
    result = svc.backfill(env.data_dir, "price_limits")
    assert result["state"] == "failed" and "不可用" in result["stopped_reason"]


def test_status_reports_days_and_route(env):
    svc.sync_day(env.data_dir, "price_limits", DAY)
    info = svc.status(env.data_dir)["datasets"]
    assert info["price_limits"] == {"days": 1, "first": "2026-11-18", "latest": "2026-11-18",
                                    "upstream_first": "2020-01-02", "source": "meoz", "usable": True}
    assert info["limit_events"]["days"] == 0


# ---- 调度与入口 ----

@pytest.fixture
def app_env(monkeypatch, tmp_path):
    sync = Mock(return_value={"state": "saved"})
    monkeypatch.setattr(ext, "sync_day", sync)
    monkeypatch.setattr(ext, "cn_now", lambda: EVENING)
    monkeypatch.setattr(ext, "is_trading_day", lambda _now=None: True)
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.scheduler = Mock()
    app.state.scheduler.get_job.return_value = object()
    app.include_router(ext.router)
    return SimpleNamespace(app=app, sync=sync, data_dir=tmp_path)


def test_jobs_register_preopen_and_after_close(app_env):
    with TestClient(app_env.app):
        calls = app_env.app.state.scheduler.add_job.call_args_list
        plans = {c.kwargs["id"]: (c.kwargs["args"][1], c.kwargs["hour"], c.kwargs["minute"]) for c in calls}
    assert plans["market_reference_preopen"] == (("price_limits", "st_status"), 9, 5)
    assert plans["market_reference_close"] == (("price_limits", "st_status", "limit_events"), "15,20", 45)
    assert app_env.app.state.scheduler.remove_job.call_count == 2


@pytest.mark.parametrize("trading, calls", [(True, 2), (False, 0), (None, 0)])
def test_sync_job_only_on_confirmed_trading_day(app_env, monkeypatch, trading, calls):
    monkeypatch.setattr(ext, "is_trading_day", lambda _now=None: trading)
    ext._sync_today(app_env.data_dir, ("price_limits", "st_status"))
    assert app_env.sync.call_count == calls


def test_sync_job_continues_after_one_dataset_fails(app_env):
    app_env.sync.side_effect = [svc.ReferenceUnavailableError("x"), RuntimeError("boom"), {"state": "saved"}]
    ext._sync_today(app_env.data_dir, tuple(svc.DATASETS))
    assert app_env.sync.call_count == 3


def test_backfill_endpoint_errors(app_env, monkeypatch):
    start = Mock(return_value={"dataset": "price_limits", "state": "running"})
    monkeypatch.setattr(ext, "start_backfill", start)
    with TestClient(app_env.app) as client:
        assert client.post("/api/market-reference/backfill", json={"dataset": "x"}).status_code == 422
        r = client.post("/api/market-reference/backfill", json={"dataset": "price_limits", "start": "2020-01-02"})
        assert r.json()["state"] == "running"
        assert start.call_args.args[1:] == ("price_limits", date(2020, 1, 2), None)
        start.side_effect = svc.ReferenceUnavailableError("不可用")
        assert client.post("/api/market-reference/backfill", json={"dataset": "price_limits"}).status_code == 503
        start.side_effect = RuntimeError("已有回填在运行")
        assert client.post("/api/market-reference/backfill", json={"dataset": "price_limits"}).status_code == 409
