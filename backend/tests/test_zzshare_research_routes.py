from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom import zzshare as extension
from app.market_time import CN_TZ
from app.plugins.zzshare import ZzshareError
from app.services import zzshare_research as service


@pytest.fixture
def radar(monkeypatch):
    now = datetime(2026, 11, 4, 14, 30, tzinfo=CN_TZ)
    ticks = [0]
    fetch = Mock(return_value=[{"trade_date": "2026-11-03", "rank": 1}])
    app = FastAPI()
    app.include_router(extension.router)
    app.state.repo = object()
    monkeypatch.setattr(extension, "_reader", service.ResearchReader())
    monkeypatch.setattr(service, "cn_now", lambda: now)
    monkeypatch.setattr(service, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(service.custom_sources, "get_provider", lambda _: SimpleNamespace(research=fetch))
    return TestClient(app), fetch, ticks


def test_cache_is_shared_without_crossing_trade_dates(radar):
    client, fetch, ticks = radar
    path = "/api/zzshare/research/ths_hot_top"
    day = {"date": "2026-11-03"}
    saved = client.get(path, params=day).json()
    assert saved["rows"] and saved["stale"] is False
    assert client.get(path, params=day).json() == saved
    fetch.assert_called_once_with("ths_hot_top", day["date"], limit=100)
    ticks[0] = 4000
    fetch.side_effect = RuntimeError("internal upstream details")
    fallback = client.get(path, params=day).json()
    assert fallback["stale"] and fallback["rows"] == saved["rows"]
    assert fallback["fetched_at"] == saved["fetched_at"]
    failed = client.get(path, params={"date": "2026-11-02"})
    assert failed.status_code == 502 and "internal upstream" not in failed.text


def test_pending_dragon_tiger_is_not_previous_day_and_history_is_scoped(radar):
    client, fetch, _ = radar
    waiting = client.get("/api/zzshare/research/lhb_list", params={"date": "2026-11-04"}).json()
    assert waiting["rows"] == [] and waiting["fetched_at"] is None and "盘后" in waiting["warning"]
    fetch.assert_not_called()
    response = client.get("/api/zzshare/research/lhb_stock_history",
                          params={"date": "2026-11-03", "code": "002086.SZ"})
    assert response.status_code == 200
    fetch.assert_called_once_with("lhb_stock_history", "2026-11-03", code="002086.SZ", limit=100)


@pytest.mark.parametrize(("kind", "params"), [
    ("lhb_detail", {"date": "2026-11-03"}),
    ("lhb_detail", {"date": "2026-11-03", "code": "../secrets"}),
    ("ths_hot_top", {"date": "2026-11-03", "code": "002086"}),
    ("plates_rank", {"date": "2026-11-05"}),
    ("other", {"date": "2026-11-03"}),
])
def test_invalid_inputs_do_not_reach_the_vendor(radar, kind, params):
    client, fetch, _ = radar
    assert client.get(f"/api/zzshare/research/{kind}", params=params).status_code == 422
    fetch.assert_not_called()


def test_multiple_readers_share_one_fetch(radar):
    client, fetch, _ = radar
    entered, release = Event(), Event()

    def pulled(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return [{"trade_date": "2026-11-03", "close": 100}]

    fetch.side_effect = pulled
    path = "/api/zzshare/research/market_sentiment?date=2026-11-03"
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(client.get, path)
        assert entered.wait(5)
        second = pool.submit(client.get, path)
        release.set()
        assert first.result().json() == second.result().json()
    fetch.assert_called_once_with("market_sentiment", "2026-11-03", start="2026-10-05")


def test_vendor_plate_categories_have_independent_caches(radar):
    client, fetch, _ = radar
    path = "/api/zzshare/research/plates_rank?date=2026-11-03"
    for suffix in ("", "&plate_type=14", "&plate_type=15", "&plate_type=17"):
        assert client.get(path + suffix).is_success
    assert [c.kwargs["plate_type"] for c in fetch.call_args_list] == [17, 14, 15]
    assert client.get(path + "&plate_type=99").status_code == 422
    assert client.get("/api/zzshare/research/ths_hot_top?date=2026-11-03&plate_type=14").status_code == 422


@pytest.mark.parametrize(("status", "expected"), [(401, 503), (403, 503), (429, 429), (500, 502)])
def test_vendor_error_status_is_actionable_without_exposing_a_payload(radar, status, expected):
    client, fetch, _ = radar
    fetch.side_effect = ZzshareError("private upstream details", status)
    response = client.get("/api/zzshare/research/ths_hot_top?date=2026-11-03")
    assert response.status_code == expected and "private" not in response.text


def test_theme_read_never_starts_collection_and_sync_is_explicit(radar, monkeypatch):
    client, _, _ = radar
    day = date(2026, 11, 3)
    monkeypatch.setattr(extension, "cn_today", lambda: day)
    get, sync = Mock(return_value={"state": "idle"}), Mock(return_value={"state": "running"})
    monkeypatch.setattr(extension, "get_themes", get)
    monkeypatch.setattr(extension, "start_themes_sync", sync)
    assert client.get("/api/zzshare/themes?date=2026-11-03").json()["state"] == "idle"
    sync.assert_not_called()
    response = client.post("/api/zzshare/themes/sync", json={"date": day.isoformat()})
    assert response.status_code == 202 and response.json()["state"] == "running"
    assert get.call_args.args == sync.call_args.args == (client.app.state.repo, day)
    assert client.get("/api/zzshare/themes?date=2026-11-04").status_code == 422
    assert client.post("/api/zzshare/themes/sync", json={"date": "2026-02-30"}).status_code == 422
    sync.assert_called_once()
