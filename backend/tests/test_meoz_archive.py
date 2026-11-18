"""猫爪当前快照盘后留档: 插件取数、写一次不改写、失败隔离与调度门禁。"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom import meoz_archive as ext
from app.market_time import CN_TZ
from app.plugins.meoz.client import MeozError
from app.plugins.meoz.provider import _FIVE_MINUTE_LABELS, ARCHIVE_DATASETS, MeozProvider
from app.services import meoz_archive as svc

DAY = date(2026, 11, 18)
AFTER_CLOSE = datetime(2026, 11, 18, 15, 40, tzinfo=CN_TZ)


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def query(self, api, params=None, fields=None, service="live"):
        self.calls.append((api, dict(params or {})))
        value = self.responses[api]
        return value(params or {}) if callable(value) else value


def _provider(responses) -> tuple[MeozProvider, FakeClient]:
    provider = MeozProvider()
    provider._client = fake = FakeClient(responses)
    return provider, fake


# ---- 插件取数 ----

def test_members_are_exploded_and_catalog_marks_concepts():
    provider, fake = _provider({
        "theme_members": [{"theme_symbol": "880904", "symbols": ["000002", "000008"]},
                          {"theme_symbol": "880081", "symbols": []}],
        "thememembers_jx": [{"theme_symbol": "801001k", "symbols": ["000012"]}],
        "theme_concept": [{"symbol": "880501", "name": "含H股"}],
        "theme_industry": [{"type": "hy", "symbol": "880981", "name": "能源", "industry_level": 1}],
    })
    assert provider.get_archive_rows("theme_members", DAY) == [
        {"theme_symbol": "880904", "symbol": "000002"}, {"theme_symbol": "880904", "symbol": "000008"}]
    assert provider.get_archive_rows("jx_members", DAY) == [{"theme_symbol": "801001k", "symbol": "000012"}]
    assert ("thememembers_jx", {"tradedate": "20261118"}) in fake.calls
    catalog = provider.get_archive_rows("theme_catalog", DAY)
    assert catalog[0] == {"type": "gn", "symbol": "880501", "name": "含H股"}
    assert catalog[1]["type"] == "hy"


def test_five_minute_labels_cover_both_sessions():
    assert len(_FIVE_MINUTE_LABELS) == 49
    assert _FIVE_MINUTE_LABELS[0] == "0930" and _FIVE_MINUTE_LABELS[-1] == "1500"
    assert "1130" in _FIVE_MINUTE_LABELS and "1305" in _FIVE_MINUTE_LABELS
    assert "1200" not in _FIVE_MINUTE_LABELS and "1300" not in _FIVE_MINUTE_LABELS


def test_thememinute_requests_every_label_for_the_day():
    provider, _ = _provider({"thememinute_jx": lambda p: [{"tradedate": p["tradedate"], "trademin": p["trademin"]}]})
    rows = provider.get_archive_rows("thememinute_jx", DAY)
    assert [r["trademin"] for r in rows] == list(_FIVE_MINUTE_LABELS)


@pytest.mark.parametrize("dataset, api", [("themefundflow_jx", "themefundflow_jx"),
                                          ("anomaly_forecast", "anomaly_forecast")])
def test_stale_upstream_date_is_rejected(dataset, api):
    provider, _ = _provider({api: [{"tradedate": "20261117", "symbol": "688613"}]})
    with pytest.raises(MeozError, match="尚未更新"):
        provider.get_archive_rows(dataset, DAY)


def test_latest_only_lists_pass_through_and_unknown_dataset_rejected():
    row = {"type": "交易所风险提示", "symbol": "605255", "startdate": "20261119", "enddate": "20261202"}
    provider, _ = _provider({"point_monitor": [row]})
    assert provider.get_archive_rows("point_monitor", DAY) == [row]
    with pytest.raises(ValueError):
        provider.get_archive_rows("daily", DAY)


def test_service_and_plugin_dataset_lists_match():
    assert svc.DATASETS == ARCHIVE_DATASETS


# ---- 留档服务 ----

@pytest.fixture
def archive(monkeypatch, tmp_path):
    rows = {ds: [{"symbol": "000001", "symbols": ["a", "b"], "n": 1}] for ds in svc.DATASETS}
    fetch = Mock(side_effect=lambda ds, day: rows[ds])
    provider = SimpleNamespace(get_archive_rows=fetch)
    monkeypatch.setattr(svc.custom_sources, "get_provider", lambda name: provider)
    monkeypatch.setattr(svc, "cn_now", lambda: AFTER_CLOSE)
    svc._last_run.clear()
    return SimpleNamespace(rows=rows, fetch=fetch, data_dir=tmp_path)


def test_saves_once_and_later_runs_do_not_refetch(archive):
    first = svc.archive_today(archive.data_dir)
    assert {r["state"] for r in first["results"].values()} == {"saved"}
    path = archive.data_dir / "meoz_archive" / "v1" / "point_monitor" / "date=2026-11-18.parquet"
    frame = pl.read_parquet(path)
    assert frame.columns == ["symbol", "symbols", "n", "fetched_at"]
    assert frame.row(0) == ("000001", '["a", "b"]', "1", "2026-11-18T15:40:00+08:00")
    archive.fetch.reset_mock()
    second = svc.archive_today(archive.data_dir)
    assert {r["state"] for r in second["results"].values()} == {"cached"}
    archive.fetch.assert_not_called()


def test_one_failed_dataset_does_not_block_others(archive):
    def fetch(ds, day):
        if ds == "theme_members":
            raise MeozError("theme_members HTTP 503")
        return [] if ds == "anomaly_forecast" else archive.rows[ds]
    archive.fetch.side_effect = fetch
    results = svc.archive_today(archive.data_dir)["results"]
    assert results["theme_members"]["state"] == "error" and "503" in results["theme_members"]["error"]
    assert results["anomaly_forecast"] == {"state": "empty"}
    assert results["point_monitor"]["state"] == "saved"
    status = svc.status(archive.data_dir)
    assert status["datasets"]["theme_members"]["days"] == 0
    assert status["datasets"]["point_monitor"] == {"days": 1, "first": "2026-11-18", "latest": "2026-11-18"}
    assert status["last_run"]["date"] == "2026-11-18"
    # 补存轮只补失败和空的数据集
    archive.fetch.side_effect = lambda ds, day: archive.rows[ds]
    archive.fetch.reset_mock()
    svc.archive_today(archive.data_dir)
    assert sorted(c.args[0] for c in archive.fetch.call_args_list) == ["anomaly_forecast", "theme_members"]


def test_refuses_before_close_and_unknown_dataset(archive, monkeypatch):
    with pytest.raises(ValueError, match="未知"):
        svc.archive_today(archive.data_dir, ("daily",))
    monkeypatch.setattr(svc, "cn_now", lambda: datetime(2026, 11, 18, 14, 59, tzinfo=CN_TZ))
    with pytest.raises(ValueError, match="15:30"):
        svc.archive_today(archive.data_dir)
    archive.fetch.assert_not_called()


def test_missing_plugin_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(svc, "cn_now", lambda: AFTER_CLOSE)
    monkeypatch.setattr(svc.custom_sources, "get_provider", Mock(side_effect=ValueError("not found")))
    with pytest.raises(svc.ArchiveUnavailableError):
        svc.archive_today(tmp_path)
    monkeypatch.setattr(svc.custom_sources, "get_provider", lambda name: SimpleNamespace())
    with pytest.raises(svc.ArchiveUnavailableError, match="不支持"):
        svc.archive_today(tmp_path)


# ---- 调度与入口 ----

@pytest.fixture
def app_env(monkeypatch, tmp_path):
    archive = Mock(return_value={"date": "2026-11-18", "results": {}})
    monkeypatch.setattr(ext, "archive_today", archive)
    monkeypatch.setattr(ext, "cn_now", lambda: AFTER_CLOSE)
    monkeypatch.setattr(ext, "is_trading_day", lambda _now=None: True)
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.scheduler = Mock()
    app.state.scheduler.get_job.return_value = object()
    app.include_router(ext.router)
    return SimpleNamespace(app=app, archive=archive, data_dir=tmp_path)


def test_scheduler_registers_after_close_and_removes_on_shutdown(app_env):
    with TestClient(app_env.app):
        kwargs = app_env.app.state.scheduler.add_job.call_args.kwargs
        assert (kwargs["hour"], kwargs["minute"], kwargs["timezone"]) == ("15,20", 40, "Asia/Shanghai")
    app_env.app.state.scheduler.remove_job.assert_called_once_with(ext._JOB_ID)


@pytest.mark.parametrize("trading, calls", [(True, 1), (False, 0), (None, 0)])
def test_scheduler_job_only_runs_on_confirmed_trading_day(app_env, monkeypatch, trading, calls):
    monkeypatch.setattr(ext, "is_trading_day", lambda _now=None: trading)
    ext._run(app_env.data_dir)
    assert app_env.archive.call_count == calls


def test_scheduler_job_swallows_missing_plugin_and_upstream_errors(app_env):
    app_env.archive.side_effect = svc.ArchiveUnavailableError("x")
    ext._run(app_env.data_dir)
    app_env.archive.side_effect = RuntimeError("boom")
    ext._run(app_env.data_dir)


def test_run_endpoint_errors_and_status(app_env, monkeypatch):
    with TestClient(app_env.app) as client:
        assert client.post("/api/meoz-archive/run").json()["date"] == "2026-11-18"
        app_env.archive.side_effect = svc.ArchiveUnavailableError("猫爪插件未就绪")
        assert client.post("/api/meoz-archive/run").status_code == 503
        app_env.archive.side_effect = ValueError("请在当日 15:30 收盘数据更新后留档")
        assert client.post("/api/meoz-archive/run").status_code == 400
        monkeypatch.setattr(ext, "is_trading_day", lambda _now=None: False)
        assert client.post("/api/meoz-archive/run").status_code == 409
        body = client.get("/api/meoz-archive/status").json()
        assert set(body["datasets"]) == set(svc.DATASETS)
