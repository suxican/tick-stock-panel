"""猫爪题材归因: theme_reason / hotstock / theme_hot_kp 的取数、盘中缓存、留档与入口错误。"""
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
from app.plugins.meoz.client import MeozError
from app.plugins.meoz.provider import MeozProvider
from app.services import meoz_hot_rank as hot
from app.services import meoz_theme_reasons as svc

DAY = date(2026, 11, 19)
INTRADAY = datetime(2026, 11, 19, 10, 0, tzinfo=CN_TZ)
AFTER = datetime(2026, 11, 19, 16, 0, tzinfo=CN_TZ)


class FakeClient:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def query(self, api, params=None, fields=None, service="live"):
        self.calls.append((api, dict(params or {})))
        key = (api, (params or {}).get("source") or (params or {}).get("type"))
        value = self.responses.get(key, self.responses.get(api, []))
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr("app.plugins.meoz.provider.time_mod.sleep", lambda s: None)


def _provider(responses):
    provider = MeozProvider()
    responses.setdefault("stockbasic", [{"code": "002242.SZ", "symbol": "002242"}])
    provider._client = fake = FakeClient(responses)
    return provider, fake


# ---------------- provider ----------------

def test_theme_reasons_merge_sources_and_pass_tradedate():
    provider, fake = _provider({
        ("theme_reason", "xgb"): [{"tradedate": "20261119", "source": "xgb", "symbol": "1x", "name": "养猪",
                                   "reason": " 生猪涨价 "}],
        ("theme_reason", "jygs"): [{"tradedate": "20261119", "source": "jygs", "symbol": "2j", "name": "AI硬件",
                                    "reason": "警报"}],
    })
    out = provider.get_theme_reasons(DAY)
    assert out["failed"] == []
    assert out["rows"] == [
        {"source": "xgb", "theme_id": "1x", "name": "养猪", "reason": "生猪涨价"},
        {"source": "jygs", "theme_id": "2j", "name": "AI硬件", "reason": "警报"},
    ]
    assert ("theme_reason", {"source": "xgb", "tradedate": "20261119"}) in fake.calls


def test_theme_reasons_one_source_failing_is_reported_not_empty():
    provider, _ = _provider({
        ("theme_reason", "xgb"): MeozError("HTTP 502"),
        ("theme_reason", "jygs"): [{"tradedate": "20261119", "symbol": "2j", "name": "AI", "reason": "r"}],
    })
    out = provider.get_theme_reasons(DAY)
    assert out["failed"] == ["xgb"]
    assert [r["source"] for r in out["rows"]] == ["jygs"]


def test_theme_reasons_all_sources_failing_raises():
    provider, _ = _provider({"theme_reason": MeozError("HTTP 502")})
    with pytest.raises(MeozError):
        provider.get_theme_reasons(DAY)


def test_hot_stocks_live_uses_row_order_and_history_uses_rank():
    provider, fake = _provider({("hotstock", "ths&normal"): [
        {"s": "002242", "n": "九阳股份", "pc": -7.21}, {"s": "600410", "n": "华胜天成", "pc": 1.0}]})
    live = provider.get_hot_stocks("ths_normal")
    assert live == [
        {"symbol": "002242.SZ", "rank": 1, "name": "九阳股份", "pct_chg": -0.0721},
        {"symbol": "600410.SH", "rank": 2, "name": "华胜天成", "pct_chg": 0.01},
    ]
    assert ("hotstock", {"type": "ths&normal"}) in fake.calls
    fake.responses[("hotstock", "ths&normal")] = [{"s": "600410", "rank": 5, "tradedate": "20261119"}]
    hist = provider.get_hot_stocks("ths_normal", DAY)
    assert hist == [{"symbol": "600410.SH", "rank": 5, "name": None, "pct_chg": None}]
    assert ("hotstock", {"type": "ths&normal", "tradedate": "20261119"}) in fake.calls
    with pytest.raises(ValueError):
        provider.get_hot_stocks("unknown")


def test_theme_hot_parses_updated_at_as_beijing_iso():
    provider, _ = _provider({"theme_hot_kp": [
        {"date": None, "rank": 1, "theme_symbol": "801001", "theme_name": "芯片", "updated_at": "2026-11-20 16:31:31"}]})
    assert provider.get_theme_hot() == [
        {"rank": 1, "theme_id": "801001", "name": "芯片", "available_at": "2026-11-20T16:31:31+08:00"}]


# ---------------- theme reasons service ----------------

@pytest.fixture
def env(monkeypatch, tmp_path):
    rows = [{"source": "xgb", "theme_id": "1x", "name": "养猪", "reason": "涨价"}]
    fetch = Mock(side_effect=lambda day: {"rows": rows, "failed": []})
    monkeypatch.setattr(svc.custom_sources, "get_provider", lambda name: SimpleNamespace(get_theme_reasons=fetch))
    monkeypatch.setattr(svc, "cn_now", lambda: INTRADAY)
    svc.reset_for_tests()
    return SimpleNamespace(fetch=fetch, rows=rows, data_dir=tmp_path)


def test_intraday_is_live_cached_and_not_archived(env, monkeypatch):
    first = svc.read(env.data_dir, DAY)
    assert (first["source"], first["final"], first["rows"]) == ("live", False, env.rows)
    assert first["fetched_at"]
    svc.read(env.data_dir, DAY)
    assert env.fetch.call_count == 1
    monkeypatch.setattr(svc, "LIVE_TTL_S", -1)
    svc.read(env.data_dir, DAY)
    assert env.fetch.call_count == 2
    assert not svc.path_of(env.data_dir, DAY).exists()


def test_after_close_archives_once(env, monkeypatch):
    monkeypatch.setattr(svc, "cn_now", lambda: AFTER)
    first = svc.read(env.data_dir, DAY)
    assert (first["source"], first["final"], first["unavailable_sources"]) == ("archive", True, [])
    assert pl.read_parquet(svc.path_of(env.data_dir, DAY))["name"].to_list() == ["养猪"]
    env.fetch.side_effect = AssertionError("留档后不应再请求")
    assert svc.read(env.data_dir, DAY)["rows"] == env.rows


def test_partial_failure_is_not_archived_and_reports_sources(env, monkeypatch):
    monkeypatch.setattr(svc, "cn_now", lambda: AFTER)
    env.fetch.side_effect = lambda day: {"rows": env.rows, "failed": ["jygs"]}
    out = svc.read(env.data_dir, DAY)
    assert (out["source"], out["unavailable_sources"]) == ("live", ["jygs"])
    assert not svc.path_of(env.data_dir, DAY).exists()


def test_before_history_start_and_empty_and_future(env, monkeypatch):
    monkeypatch.setattr(svc, "cn_now", lambda: AFTER)
    assert svc.read(env.data_dir, date(2025, 12, 31))["rows"] == []
    env.fetch.assert_not_called()
    env.fetch.side_effect = lambda day: {"rows": [], "failed": []}
    assert svc.read(env.data_dir, date(2026, 11, 18))["rows"] == []
    assert not svc.path_of(env.data_dir, date(2026, 11, 18)).exists()
    with pytest.raises(ValueError):
        svc.read(env.data_dir, date(2026, 11, 20))


def test_missing_plugin_is_unavailable(monkeypatch, tmp_path):
    svc.reset_for_tests()
    monkeypatch.setattr(svc, "cn_now", lambda: INTRADAY)
    monkeypatch.setattr(svc.custom_sources, "get_provider", Mock(side_effect=ValueError("x")))
    with pytest.raises(svc.ThemeReasonsUnavailableError):
        svc.read(tmp_path, DAY)


# ---------------- hot rank service ----------------

def _hot_provider(stocks=None, themes=None):
    def get_hot_stocks(kind, day=None):
        value = (stocks or {}).get(kind, [])
        if isinstance(value, Exception):
            raise value
        return value
    theme_fn = Mock(side_effect=themes if isinstance(themes, Exception) else (lambda: themes or []))
    return SimpleNamespace(get_hot_stocks=get_hot_stocks, get_theme_hot=theme_fn)


def test_hot_rank_each_list_fails_independently_and_carries_fetched_at(monkeypatch):
    hot.reset_for_tests()
    provider = _hot_provider(
        stocks={"ths_normal": [{"symbol": "002242.SZ", "rank": 1, "name": "九阳", "pct_chg": 0.01}],
                "dfcf_renqi": MeozError("HTTP 502")},
        themes=[{"rank": 1, "theme_id": "801001", "name": "芯片", "available_at": "2026-11-19T16:31:31+08:00"}])
    monkeypatch.setattr(hot.custom_sources, "get_provider", lambda name: provider)
    monkeypatch.setattr(hot, "cn_now", lambda: INTRADAY)
    out = hot.read()
    assert out["fetched_at"] == INTRADAY.isoformat(timespec="seconds")
    assert out["stocks"]["ths_normal"]["available"] is True
    assert out["stocks"]["ths_normal"]["rows"][0]["symbol"] == "002242.SZ"
    assert out["stocks"]["dfcf_renqi"] == {"available": False, "rows": []}
    assert out["stocks"]["ths_trend"] == {"available": True, "rows": []}  # 成功但无数据 ≠ 不可用
    assert out["themes"]["available"] is True and out["themes"]["rows"][0]["name"] == "芯片"
    hot.read()
    assert provider.get_theme_hot.call_count == 1  # 60 秒缓存


def test_hot_rank_missing_plugin_is_unavailable(monkeypatch):
    hot.reset_for_tests()
    monkeypatch.setattr(hot.custom_sources, "get_provider", Mock(side_effect=ValueError("x")))
    with pytest.raises(hot.HotRankUnavailableError):
        hot.read()


def test_latest_falls_back_over_weekend_to_last_day_with_data(env, monkeypatch):
    sat = date(2026, 11, 21)
    monkeypatch.setattr(svc, "cn_now", lambda: datetime(2026, 11, 21, 9, 0, tzinfo=CN_TZ))
    env.fetch.side_effect = lambda day: {"rows": env.rows if day == DAY else [], "failed": []}
    out = svc.read_latest(env.data_dir, sat)
    assert (out["date"], out["rows"], out["final"]) == ("2026-11-19", env.rows, True)
    assert date(2026, 11, 22) not in [c.args[0] for c in env.fetch.call_args_list]
    assert date(2026, 11, 20) in [c.args[0] for c in env.fetch.call_args_list]  # 周五无数据继续回退


def test_latest_on_weekday_keeps_polling_and_prefers_today(env, monkeypatch):
    today = date(2026, 11, 20)
    monkeypatch.setattr(svc, "cn_now", lambda: datetime(2026, 11, 20, 8, 0, tzinfo=CN_TZ))
    env.fetch.side_effect = lambda day: {"rows": env.rows if day == DAY else [], "failed": []}
    out = svc.read_latest(env.data_dir, today)
    assert (out["date"], out["final"]) == ("2026-11-19", False)
    monkeypatch.setattr(svc, "LIVE_TTL_S", -1)
    env.fetch.side_effect = lambda day: {"rows": env.rows, "failed": []}
    assert svc.read_latest(env.data_dir, today)["date"] == "2026-11-20"


def test_latest_does_not_fall_back_when_a_source_failed(env, monkeypatch):
    monkeypatch.setattr(svc, "cn_now", lambda: datetime(2026, 11, 21, 9, 0, tzinfo=CN_TZ))
    env.fetch.side_effect = lambda day: {"rows": [], "failed": ["xgb"]}
    out = svc.read_latest(env.data_dir, date(2026, 11, 21))
    assert (out["date"], out["unavailable_sources"]) == ("2026-11-21", ["xgb"])


# ---------------- routes ----------------

def test_routes_map_errors(monkeypatch, tmp_path):
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(ext.router)
    reader = Mock(return_value={"date": "2026-11-19", "source": "live", "final": False, "rows": []})
    hot_reader = Mock(return_value={"fetched_at": "x", "stocks": {}, "themes": {}})
    monkeypatch.setattr(ext, "read_theme_reasons", reader)
    monkeypatch.setattr(ext, "read_hot_rank", hot_reader)
    with TestClient(app) as client:
        assert client.get("/api/meoz/theme-reasons", params={"date": "2026-11-19"}).json()["source"] == "live"
        assert client.get("/api/meoz/hot-rank").json()["fetched_at"] == "x"
        for error, code in ((ValueError("未来"), 422), (svc.ThemeReasonsUnavailableError("未就绪"), 503),
                            (RuntimeError("502"), 502)):
            reader.side_effect = error
            assert client.get("/api/meoz/theme-reasons", params={"date": "2026-11-19"}).status_code == code
        for error, code in ((hot.HotRankUnavailableError("未就绪"), 503), (RuntimeError("x"), 502)):
            hot_reader.side_effect = error
            assert client.get("/api/meoz/hot-rank").status_code == code


def test_theme_authenticity_route_returns_symbol_summary(monkeypatch, tmp_path):
    """首板展开区: 单只候选所在的最正宗精选板块; 来源不可用为 503 (前端显示「来源不可用」)。"""
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(ext.router)
    fetch = Mock(return_value={"688388.SH": [{"theme_id": "801004k", "theme_name": "锂电池", "change_pct": -0.0345}]})
    monkeypatch.setattr(ext.theme_authenticity, "fetch_authenticity", fetch)
    with TestClient(app) as client:
        body = client.get("/api/meoz/theme-authenticity", params={"symbol": "688388.SH"}).json()
        assert body == {"available": True, "themes": [{"theme_id": "801004k", "theme_name": "锂电池", "change_pct": -0.0345}]}
        fetch.assert_called_with(["688388.SH"])
        fetch.return_value = {"600000.SH": []}
        assert client.get("/api/meoz/theme-authenticity", params={"symbol": "600000.SH"}).json() == {"available": True, "themes": []}
        fetch.return_value = None
        assert client.get("/api/meoz/theme-authenticity", params={"symbol": "600000.SH"}).status_code == 503
        assert client.get("/api/meoz/theme-authenticity", params={"symbol": " "}).status_code == 422
