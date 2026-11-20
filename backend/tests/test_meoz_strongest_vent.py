"""猫爪最强风口 (fengk_kp): provider 字段规整、服务缓存与入口错误映射。"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom import meoz_reasons as ext
from app.market_time import cn_now
from app.plugins.meoz.provider import MeozProvider
from app.services import meoz_strongest_vent as svc

SAMPLE = {"symbol": "000530", "name": "冰山冷热", "strength": "4893", "pct_chg": "10.03",
          "main_net_amount": "967921794", "main_buy_amount": "1598063994",
          "main_sell_amount": "-630142200", "signal_time": "1795138380", "selected_themes": "PCB、芯片"}


class FakeClient:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def query(self, api, params=None, fields=None, service="live"):
        self.calls.append((api, dict(params or {})))
        if api == "stockbasic":
            return [{"code": "000530.SZ", "symbol": "000530"}]
        return self.rows


def _provider(rows):
    provider = MeozProvider()
    provider._client = fake = FakeClient(rows)
    return provider, fake


@pytest.fixture(autouse=True)
def _reset():
    svc.reset_for_tests()
    yield
    svc.reset_for_tests()


# ---------------- provider ----------------

def test_provider_normalizes_row():
    provider, fake = _provider([SAMPLE])
    rows = provider.get_strongest_vent()
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "000530.SZ"
    assert row["name"] == "冰山冷热"
    assert row["strength"] == 4893
    assert row["pct_chg"] == pytest.approx(0.1003)
    assert row["main_net_amount"] == pytest.approx(967921794)
    assert row["main_buy_amount"] == pytest.approx(1598063994)
    assert row["main_sell_amount"] == pytest.approx(-630142200)
    assert row["signal_at"].endswith("+08:00")
    assert row["themes"] == ["PCB", "芯片"]
    assert ("fengk_kp", {}) in fake.calls


def test_provider_empty_themes_and_skips_bad_rows():
    provider, _ = _provider([
        {**SAMPLE, "selected_themes": None},
        {**SAMPLE, "symbol": "000531", "strength": None},
        {**SAMPLE, "symbol": "12345", "strength": "10"},
        {**SAMPLE, "selected_themes": "、A、、B、"},
    ])
    rows = provider.get_strongest_vent()
    assert [r["themes"] for r in rows] == [[], ["A", "B"]]


def test_provider_passes_tradedate_for_history():
    provider, fake = _provider([SAMPLE])
    provider.get_strongest_vent(date(2026, 11, 19))
    assert ("fengk_kp", {"tradedate": "20261119"}) in fake.calls


def test_provider_all_rows_unparsable_raises():
    from app.plugins.meoz.client import MeozError

    provider, _ = _provider([{**SAMPLE, "symbol": None}, {**SAMPLE, "strength": None}])
    with pytest.raises(MeozError):
        provider.get_strongest_vent()
    empty, _ = _provider([])
    assert empty.get_strongest_vent() == []


def test_provider_dedups_themes_keeping_order():
    provider, _ = _provider([{**SAMPLE, "selected_themes": "A、B、A、C、B"}])
    assert provider.get_strongest_vent()[0]["themes"] == ["A", "B", "C"]


# ---------------- service ----------------

def _fake_provider(rows=None):
    return SimpleNamespace(get_strongest_vent=Mock(return_value=rows if rows is not None else [{"symbol": "a"}]))


def test_live_cache_hit(monkeypatch):
    provider = _fake_provider()
    monkeypatch.setattr(svc.custom_sources, "get_provider", lambda name: provider)
    first = svc.read(None)
    second = svc.read(None)
    assert provider.get_strongest_vent.call_count == 1
    assert first["trade_date"] is None and first["rows"] == [{"symbol": "a"}] and first["fetched_at"]
    assert second is first


def test_history_caches_only_non_empty(monkeypatch):
    day = date(2026, 11, 19)
    provider = _fake_provider([])
    monkeypatch.setattr(svc.custom_sources, "get_provider", lambda name: provider)
    assert svc.read(day)["rows"] == []
    svc.read(day)
    assert provider.get_strongest_vent.call_count == 2
    provider.get_strongest_vent.return_value = [{"symbol": "a"}]
    assert svc.read(day)["trade_date"] == "2026-11-19"
    svc.read(day)
    assert provider.get_strongest_vent.call_count == 3
    provider.get_strongest_vent.assert_called_with(day)


def test_today_not_cached_past_day_cached(monkeypatch):
    provider = _fake_provider([{"symbol": "a"}])
    monkeypatch.setattr(svc.custom_sources, "get_provider", lambda name: provider)
    today = cn_now().date()
    svc.read(today)
    svc.read(today)
    assert provider.get_strongest_vent.call_count == 2
    assert today not in svc._history
    past = today - timedelta(days=1)
    svc.read(past)
    svc.read(past)
    assert provider.get_strongest_vent.call_count == 3


def test_history_cache_bounded(monkeypatch):
    provider = _fake_provider()
    monkeypatch.setattr(svc.custom_sources, "get_provider", lambda name: provider)
    base = date(2026, 1, 1)
    for i in range(20):
        svc.read(base + timedelta(days=i))
    assert len(svc._history) == 16


def test_future_date_rejected(monkeypatch):
    provider = _fake_provider()
    monkeypatch.setattr(svc.custom_sources, "get_provider", lambda name: provider)
    with pytest.raises(ValueError):
        svc.read(cn_now().date() + timedelta(days=1))
    provider.get_strongest_vent.assert_not_called()


def test_provider_missing_is_unavailable(monkeypatch):
    def boom(name):
        raise ValueError("no plugin")

    monkeypatch.setattr(svc.custom_sources, "get_provider", boom)
    with pytest.raises(svc.StrongestVentUnavailableError):
        svc.read(None)
    monkeypatch.setattr(svc.custom_sources, "get_provider", lambda name: SimpleNamespace())
    with pytest.raises(svc.StrongestVentUnavailableError):
        svc.read(None)


# ---------------- route ----------------

def test_route_502_logs_warning(monkeypatch, caplog):
    app = FastAPI()
    app.include_router(ext.router)
    monkeypatch.setattr(ext, "read_strongest_vent", Mock(side_effect=RuntimeError("x")))
    with TestClient(app) as client, caplog.at_level("WARNING"):
        assert client.get("/api/meoz/strongest-vent").status_code == 502
    assert any("猫爪最强风口取数失败" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)


def test_route_maps_errors(monkeypatch):
    app = FastAPI()
    app.include_router(ext.router)
    reader = Mock(return_value={"fetched_at": "x", "trade_date": None, "rows": []})
    monkeypatch.setattr(ext, "read_strongest_vent", reader)
    with TestClient(app) as client:
        assert client.get("/api/meoz/strongest-vent").json()["fetched_at"] == "x"
        reader.assert_called_with(None)
        client.get("/api/meoz/strongest-vent", params={"date": "2026-11-19"})
        reader.assert_called_with(date(2026, 11, 19))
        for error, code in ((ValueError("未来"), 422), (svc.StrongestVentUnavailableError("未就绪"), 503),
                            (RuntimeError("x"), 502)):
            reader.side_effect = error
            resp = client.get("/api/meoz/strongest-vent")
            assert resp.status_code == code
        assert "猫爪最强风口暂时不可用" in resp.json()["detail"]
