"""猫爪个股涨停归因: 插件取数、盘中实时缓存、收盘后留档与入口错误。"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom import meoz_reasons as ext
from app.market_time import CN_TZ
from app.plugins.meoz.provider import MeozProvider
from app.services import meoz_limit_reasons as svc

DAY = date(2026, 11, 19)
INTRADAY = datetime(2026, 11, 19, 10, 0, tzinfo=CN_TZ)
AFTER = datetime(2026, 11, 19, 16, 0, tzinfo=CN_TZ)


class FakeClient:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def query(self, api, params=None, fields=None, service="live"):
        self.calls.append((api, dict(params or {})))
        return {"stockbasic": [{"code": "000678.SZ", "symbol": "000678"}]}.get(api, self.rows)


def test_provider_keeps_limit_up_rows_with_any_reason(monkeypatch):
    rows = [
        {"tradedate": "20261119", "symbol": "000678", "type": "u", "reason_main_kpl": "机器人概念+减速器",
         "reason_main_jygs": " 机器人轴承 ", "reason_jygs": "1、详情", "reason_xgb": None},
        {"tradedate": "20261119", "symbol": "600001", "type": "ub", "reason_main_kpl": None, "reason_main_jygs": None,
         "reason_jygs": None, "reason_xgb": "选股宝原因"},
        {"tradedate": "20261119", "symbol": "600002", "type": "u", "reason_main_kpl": None, "reason_main_jygs": None,
         "reason_jygs": None, "reason_xgb": None},                                   # 无任何归因
        {"tradedate": "20261119", "symbol": "600003", "type": None, "reason_main_kpl": "非涨停"},  # 非涨停
    ]
    import app.plugins.meoz.provider as mp
    monkeypatch.setattr(mp, "cn_now", lambda: INTRADAY)
    provider = MeozProvider()
    provider._client = fake = FakeClient(rows)
    out = provider.get_limit_reasons(DAY)
    assert out == [
        {"symbol": "000678.SZ", "kpl": "机器人概念+减速器", "jygs": "机器人轴承", "jygs_detail": "1、详情", "xgb": None},
        {"symbol": "600001.SH", "kpl": None, "jygs": None, "jygs_detail": None, "xgb": "选股宝原因"},
    ]
    assert ("screening", {}) in fake.calls  # 当日走实时
    provider.get_limit_reasons(date(2026, 11, 18))
    assert ("screening", {"tradedate": "20261118"}) in fake.calls


@pytest.fixture
def env(monkeypatch, tmp_path):
    rows = [{"symbol": "000678.SZ", "kpl": "机器人", "jygs": None, "jygs_detail": None, "xgb": "原因"}]
    fetch = Mock(side_effect=lambda day: rows)
    monkeypatch.setattr(svc.custom_sources, "get_provider", lambda name: SimpleNamespace(get_limit_reasons=fetch))
    monkeypatch.setattr(svc, "cn_now", lambda: INTRADAY)
    svc.reset_for_tests()
    return SimpleNamespace(fetch=fetch, rows=rows, data_dir=tmp_path)


def test_intraday_is_live_cached_and_not_archived(env, monkeypatch):
    first = svc.read(env.data_dir, DAY)
    assert (first["source"], first["final"], first["rows"]) == ("live", False, env.rows)
    svc.read(env.data_dir, DAY)
    assert env.fetch.call_count == 1  # 60 秒缓存
    monkeypatch.setattr(svc, "LIVE_TTL_S", -1)
    svc.read(env.data_dir, DAY)
    assert env.fetch.call_count == 2
    assert not svc.path_of(env.data_dir, DAY).exists()


def test_after_close_archives_once_and_history_reads_archive(env, monkeypatch):
    monkeypatch.setattr(svc, "cn_now", lambda: AFTER)
    first = svc.read(env.data_dir, DAY)
    assert (first["source"], first["final"]) == ("archive", True)
    assert pl.read_parquet(svc.path_of(env.data_dir, DAY))["symbol"].to_list() == ["000678.SZ"]
    env.fetch.side_effect = AssertionError("留档后不应再请求")
    assert svc.read(env.data_dir, DAY)["rows"] == env.rows


def test_empty_upstream_is_not_archived_and_future_rejected(env, monkeypatch):
    monkeypatch.setattr(svc, "cn_now", lambda: AFTER)
    env.fetch.side_effect = lambda day: []
    assert svc.read(env.data_dir, date(2026, 11, 18))["rows"] == []
    assert not svc.path_of(env.data_dir, date(2026, 11, 18)).exists()
    with pytest.raises(ValueError):
        svc.read(env.data_dir, date(2026, 11, 20))


def test_missing_plugin_is_unavailable(monkeypatch, tmp_path):
    svc.reset_for_tests()
    monkeypatch.setattr(svc, "cn_now", lambda: INTRADAY)
    monkeypatch.setattr(svc.custom_sources, "get_provider", Mock(side_effect=ValueError("x")))
    with pytest.raises(svc.ReasonsUnavailableError):
        svc.read(tmp_path, DAY)


def test_route_maps_errors(monkeypatch, tmp_path):
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(ext.router)
    reader = Mock(return_value={"date": "2026-11-19", "source": "live", "final": False, "rows": []})
    monkeypatch.setattr(ext, "read", reader)
    with TestClient(app) as client:
        assert client.get("/api/meoz/limit-reasons", params={"date": "2026-11-19"}).json()["source"] == "live"
        for error, code in ((ValueError("未来"), 422), (svc.ReasonsUnavailableError("未就绪"), 503), (RuntimeError("502"), 502)):
            reader.side_effect = error
            assert client.get("/api/meoz/limit-reasons", params={"date": "2026-11-19"}).status_code == code
