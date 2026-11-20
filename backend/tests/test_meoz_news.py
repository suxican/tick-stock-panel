"""猫爪新闻 (news): provider 字段规整、服务排序/缓存与入口错误映射。全部使用假客户端, 不调用真实接口。"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom import meoz_reasons as ext
from app.plugins.meoz.provider import MeozProvider
from app.services import meoz_news as svc

SAMPLE = {"published_at": None, "display_at": "2026-11-20T21:01:05", "time_label": "21:01",
          "source": "yicai_home", "source_name": "第一财经头条",
          "title": "逼近20万亿元！详解前10月全国财政数据", "summary": None,
          "url": "https://www.yicai.com/news/103448373.html", "image": "https://img/x.jpg",
          "rank": 1, "source_id": "103448373", "item_type": "link", "updated_at": "2026-11-20T21:01:05"}


class FakeClient:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def query(self, api, params=None, fields=None, service="live"):
        self.calls.append((api, dict(params or {})))
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
    rows = provider.get_news()
    assert len(rows) == 1
    row = rows[0]
    assert row["display_at"] == "2026-11-20T21:01:05+08:00"
    assert "image" not in row
    assert row["id"] == "yicai_home:103448373"
    assert row["source"] == "yicai_home" and row["source_name"] == "第一财经头条"
    assert row["title"] == SAMPLE["title"] and row["summary"] is None
    assert row["url"] == SAMPLE["url"] and row["time_label"] == "21:01" and row["item_type"] == "link"
    assert row["published_at"] is None
    assert set(row) == {"id", "published_at", "display_at", "time_label", "source", "source_name",
                        "title", "summary", "url", "item_type"}
    assert ("news", {"limit": 50}) in fake.calls


def test_provider_passes_limit():
    provider, fake = _provider([SAMPLE])
    provider.get_news(20)
    assert ("news", {"limit": 20}) in fake.calls


def test_provider_skips_bad_rows():
    provider, _ = _provider([
        {**SAMPLE, "title": "  "},
        {**SAMPLE, "title": None},
        {**SAMPLE, "display_at": "not-a-time"},
        {**SAMPLE, "display_at": None},
        {**SAMPLE, "source_id": "2", "title": "好行", "summary": " 摘要 "},
    ])
    rows = provider.get_news()
    assert [r["title"] for r in rows] == ["好行"]
    assert rows[0]["summary"] == "摘要"


def test_provider_all_rows_unparsable_raises():
    from app.plugins.meoz.client import MeozError

    provider, _ = _provider([{**SAMPLE, "title": None}, {**SAMPLE, "display_at": "bad"}])
    with pytest.raises(MeozError):
        provider.get_news()
    empty, _ = _provider([])
    assert empty.get_news() == []


def test_provider_dedups_by_id_keeping_first():
    provider, _ = _provider([SAMPLE, {**SAMPLE, "summary": "dup"}, {**SAMPLE, "source_id": "2", "title": "别条"}])
    rows = provider.get_news()
    assert [r["id"] for r in rows] == ["yicai_home:103448373", "yicai_home:2"]
    assert rows[0]["summary"] is None


# ---------------- service ----------------

def _row(source, at, title, name=None):
    return {"id": f"{source}:{title}", "display_at": at, "source": source,
            "source_name": name or source, "title": title}


def _fake_provider(rows):
    return SimpleNamespace(get_news=Mock(return_value=rows))


def test_sorted_desc_stable_and_sources_dedup(monkeypatch):
    rows = [
        _row("a", "2026-11-20T10:00:00+08:00", "a1", "来源A"),
        _row("b", "2026-11-20T11:00:00+08:00", "b1", "来源B"),
        _row("a", "2026-11-20T11:00:00+08:00", "a2", "来源A"),
        _row("c", "2026-11-20T09:00:00+08:00", "c1", "来源C"),
    ]
    provider = _fake_provider(rows)
    monkeypatch.setattr(svc.custom_sources, "get_provider", lambda name: provider)
    out = svc.read()
    assert [r["title"] for r in out["rows"]] == ["b1", "a2", "a1", "c1"]
    assert out["sources"] == [{"source": "a", "source_name": "来源A"},
                              {"source": "b", "source_name": "来源B"},
                              {"source": "c", "source_name": "来源C"}]
    assert out["fetched_at"]
    provider.get_news.assert_called_with(50)


def test_cache_hit_per_limit(monkeypatch):
    provider = _fake_provider([_row("a", "2026-11-20T10:00:00+08:00", "a1")])
    monkeypatch.setattr(svc.custom_sources, "get_provider", lambda name: provider)
    first = svc.read()
    assert svc.read() is first
    assert provider.get_news.call_count == 1
    svc.read(20)
    assert provider.get_news.call_count == 2


@pytest.mark.parametrize("limit", [0, 101, -1])
def test_limit_out_of_range(monkeypatch, limit):
    provider = _fake_provider([])
    monkeypatch.setattr(svc.custom_sources, "get_provider", lambda name: provider)
    with pytest.raises(ValueError):
        svc.read(limit)
    provider.get_news.assert_not_called()


def test_provider_missing_is_unavailable(monkeypatch):
    def boom(name):
        raise ValueError("no plugin")

    monkeypatch.setattr(svc.custom_sources, "get_provider", boom)
    with pytest.raises(svc.NewsUnavailableError):
        svc.read()
    monkeypatch.setattr(svc.custom_sources, "get_provider", lambda name: SimpleNamespace())
    with pytest.raises(svc.NewsUnavailableError):
        svc.read()


# ---------------- route ----------------

def test_route_502_logs_warning(monkeypatch, caplog):
    app = FastAPI()
    app.include_router(ext.router)
    monkeypatch.setattr(ext, "read_news", Mock(side_effect=RuntimeError("x")))
    with TestClient(app) as client, caplog.at_level("WARNING"):
        assert client.get("/api/meoz/news").status_code == 502
    assert any("猫爪新闻取数失败" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)


def test_route_maps_errors(monkeypatch):
    app = FastAPI()
    app.include_router(ext.router)
    reader = Mock(return_value={"fetched_at": "x", "rows": [], "sources": []})
    monkeypatch.setattr(ext, "read_news", reader)
    with TestClient(app) as client:
        assert client.get("/api/meoz/news").json()["fetched_at"] == "x"
        reader.assert_called_with(50)
        client.get("/api/meoz/news", params={"limit": 20})
        reader.assert_called_with(20)
        for error, code in ((ValueError("limit"), 422), (svc.NewsUnavailableError("未就绪"), 503),
                            (RuntimeError("x"), 502)):
            reader.side_effect = error
            resp = client.get("/api/meoz/news")
            assert resp.status_code == code
        assert "猫爪新闻暂时不可用" in resp.json()["detail"]
