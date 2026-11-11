"""资讯分析·公告排雷测试 (不依赖真实网络)。

覆盖: 标题风险规则、巨潮客户端的记录规范化/翻页/重试/不完整判定、
按股同步的完整性判定与增量合并、单股失败隔离、机构代码表本地副本、
风险列表的窗口过滤与排序、路由。
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom import news as extension
from app.market_time import CN_TZ
from app.plugins.cninfo import client as cn
from app.services import news_announcements as na

DAYS = ["2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10"]


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=CN_TZ).timestamp() * 1000)


@pytest.mark.parametrize(("title", "kind"), [
    ("关于收到中国证监会立案告知书的公告", "penalty"),
    ("关于公司股票被实施退市风险警示的公告", "delist"),
    ("关于申请撤销退市风险警示的公告", None),
    ("关于对深圳证券交易所关注函回复的公告", "inquiry"),
    ("关于向特定对象发行股票的审核问询函回复的提示性公告", None),
    ("关于控股股东所持股份被司法冻结的公告", "freeze"),
    ("关于持股5%以上股东减持股份预披露公告", "reduce_plan"),
    ("股东减持股份结果公告", "reduce"),
    ("关于控股股东自愿承诺不减持公司股份的公告", None),
    ("关于首次公开发行前已发行股份解除限售并上市流通的提示性公告", "unlock"),
    ("关于2025年限制性股票激励计划第一个解除限售期解除限售股份上市流通的提示性公告", None),
    ("2026年度业绩预亏公告", "loss"),
    ("关于公司股票交易异常波动的公告", "abnormal"),
    ("保荐机构关于公司股东减持股份的核查意见", None),
    ("第六届董事会第十三次会议决议公告", None),
])
def test_classify_titles(title, kind):
    hit = na.classify(title)
    assert (hit[0] if hit else None) == kind


def _raw(ann_id: str, title: str, at: str, code: str = "300033") -> dict:
    return {"announcementId": ann_id, "secCode": code, "secName": "样例", "announcementTitle": title,
            "announcementTime": _ms(at), "adjunctUrl": f"finalpage/{at[:10]}/{ann_id}.PDF"}


def _client(handler) -> cn.CninfoClient:
    sleeps: list[float] = []
    client = cn.CninfoClient(sleep=sleeps.append)
    client._http = httpx.Client(base_url=cn.BASE_URL, transport=httpx.MockTransport(handler))
    client.sleeps = sleeps
    return client


def test_client_pages_retries_and_normalizes():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        calls.append(form["pageNum"])
        if len(calls) == 1:
            return httpx.Response(504, text="<html>gateway</html>")
        page = int(form["pageNum"])
        assert form["stock"] == "300033%2Cgssz0300033" and form["column"] == "szse"
        rows = [_raw(f"{page}-{i}", f"<em>公告</em>{page}-{i}", "2026-11-10T21:03:20") for i in range(30 if page == 1 else 5)]
        rows.append({**_raw("bond", "债券", "2026-11-10T00:00:00"), "secCode": "149001X"})
        return httpx.Response(200, json={"announcements": rows, "totalAnnouncement": 35, "hasMore": page == 1})

    client = _client(handler)
    items = client.stock_announcements("300033.SZ", "gssz0300033", date(2026, 11, 1), date(2026, 11, 10))
    assert calls == ["1", "1", "2"] and client.sleeps == [2.0, 1.0]
    assert len(items) == 35
    first = next(i for i in items if i["id"] == "1-0")
    assert first["title"] == "公告1-0" and first["has_time"] is True
    assert first["published_at"] == "2026-11-10T21:03:20+08:00"
    assert first["url"] == "https://static.cninfo.com.cn/finalpage/2026-11-10/1-0.PDF"


def test_client_rejects_truncated_listing_and_persistent_errors():
    client = _client(lambda r: httpx.Response(200, json={"announcements": [], "totalAnnouncement": 40, "hasMore": False}))
    with pytest.raises(cn.CninfoError, match="不完整"):
        client.stock_announcements("600519.SH", "gssh0600519", date(2026, 11, 1), date(2026, 11, 10))
    client = _client(lambda r: httpx.Response(504))
    with pytest.raises(cn.CninfoError, match="504"):
        client.stock_announcements("600519.SH", "gssh0600519", date(2026, 11, 1), date(2026, 11, 10))
    assert client.sleeps == [2.0, 5.0, 10.0, 20.0]
    client = _client(lambda r: httpx.Response(403))
    with pytest.raises(cn.CninfoError, match="403"):
        client.stock_announcements("600519.SH", "gssh0600519", date(2026, 11, 1), date(2026, 11, 10))
    assert client.sleeps == []
    with pytest.raises(ValueError):
        client.stock_announcements("600519", "x", date(2026, 11, 1), date(2026, 11, 10))


class _Fake:
    def __init__(self, items: dict[str, list[dict]], fail: set[str] = frozenset(), org_fail: bool = False):
        self.items, self.fail, self.org_fail = items, fail, org_fail
        self.calls: list[tuple] = []

    def org_ids(self):
        if self.org_fail:
            raise cn.CninfoError("巨潮返回 HTTP 504")
        return {"300033": "o1", "600519": "o2", "000001": "o3"}

    def stock_announcements(self, symbol, org_id, start, end):
        self.calls.append((symbol, start.isoformat(), end.isoformat()))
        if symbol in self.fail:
            raise cn.CninfoError("巨潮返回 HTTP 504")
        return [cn._record(r) for r in self.items.get(symbol, [])
                if start.isoformat() <= datetime.fromtimestamp(r["announcementTime"] / 1000, CN_TZ).date().isoformat() <= end.isoformat()]

    def close(self):
        pass


def _wait() -> None:
    deadline = time.monotonic() + 10
    while na._task is not None and na._task.state == "running":
        assert time.monotonic() < deadline, "同步线程未结束"
        time.sleep(0.01)


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    for day in DAYS:
        (tmp_path / "kline_daily" / f"date={day}").mkdir(parents=True)
    state = SimpleNamespace(now=datetime(2026, 11, 11, 8, 0, tzinfo=CN_TZ), fake=None)
    monkeypatch.setattr(na, "LOOKBACK_SESSIONS", 3)
    monkeypatch.setattr(na, "cn_now", lambda: state.now)
    monkeypatch.setattr(na, "_client", lambda: state.fake)
    monkeypatch.setattr(na, "_task", None)

    def run(target: date, symbols: list[str]) -> dict:
        result = na.start_sync(tmp_path, target, symbols)
        _wait()
        return result
    return tmp_path, state, run


ITEMS = {
    "300033.SZ": [
        _raw("a1", "关于持股5%以上股东减持股份预披露公告", "2026-11-06T18:00:00"),
        _raw("a2", "关于公司股票交易异常波动的公告", "2026-11-09T00:00:00"),
        _raw("a3", "第六届董事会决议公告", "2026-11-10T19:00:00"),
        _raw("a0", "关于收到立案告知书的公告", "2026-11-02T00:00:00"),
    ],
    "600519.SH": [_raw("b1", "关于对上海证券交易所问询函回复的公告", "2026-11-10T00:00:00", "600519")],
}


def test_sync_then_read_window_and_order(env):
    data_dir, state, run = env
    state.fake = _Fake(ITEMS)
    symbols = ["300033.SZ", "600519.SH", "bad"]
    assert run(date(2026, 11, 10), symbols)["started"] is True
    assert na._sync_status() == {"state": "ready", "message": "", "done": 2, "total": 2, "failed": 0}
    assert state.fake.calls == [("300033.SZ", "2026-11-06", "2026-11-10"), ("600519.SH", "2026-11-06", "2026-11-10")]
    result = na.get_risks(data_dir, date(2026, 11, 10), symbols)
    assert result["start"] == "2026-11-06" and result["symbols"] == 2
    assert result["missing"] == [] and result["partial"] == []
    # 11-02 的立案公告在窗口外; 级别优先, 同级别新公告在前
    assert [(r["id"], r["type"]) for r in result["rows"]] == [("b1", "inquiry"), ("a1", "reduce_plan"), ("a2", "abnormal")]
    assert result["counts"]["inquiry"] == 1 and result["counts"]["penalty"] == 0
    saved = json.loads((data_dir / "news_announcements" / "300033.SZ.json").read_text(encoding="utf-8"))
    assert len(saved["items"]) == 3  # 未命中风险的公告也保存, 规则调整不必重拉


def test_only_non_final_days_are_refetched_and_merged(env):
    data_dir, state, run = env
    state.now = datetime(2026, 11, 10, 20, 0, tzinfo=CN_TZ)  # 当晚: 11-10 尚不完整
    state.fake = _Fake({"300033.SZ": ITEMS["300033.SZ"][:2]})
    run(date(2026, 11, 10), ["300033.SZ"])
    state.now = datetime(2026, 11, 11, 8, 0, tzinfo=CN_TZ)
    state.fake = _Fake(ITEMS)
    run(date(2026, 11, 10), ["300033.SZ"])
    assert state.fake.calls == [("300033.SZ", "2026-11-10", "2026-11-10")]
    ids = {i["id"] for i in json.loads((data_dir / "news_announcements" / "300033.SZ.json").read_text(encoding="utf-8"))["items"]}
    assert ids == {"a1", "a2", "a3"}
    assert run(date(2026, 11, 10), ["300033.SZ"]) == {"started": False, "message": "自选股的公告已是完整快照。"}


def test_failed_symbol_is_isolated_and_reported(env):
    data_dir, state, run = env
    state.fake = _Fake(ITEMS, fail={"600519.SH"})
    run(date(2026, 11, 10), ["300033.SZ", "600519.SH", "430047.BJ"])
    status = na._sync_status()
    assert status["state"] == "error" and status["failed"] == 2  # 600519 请求失败, 430047 无机构代码
    result = na.get_risks(data_dir, date(2026, 11, 10), ["300033.SZ", "600519.SH"])
    assert result["missing"] == ["600519.SH"] and len(result["rows"]) == 2


def test_org_ids_use_local_copy_and_stale_fallback(env):
    data_dir, state, run = env
    state.fake = _Fake(ITEMS)
    run(date(2026, 11, 10), ["300033.SZ"])
    state.now = datetime(2026, 11, 30, 8, 0, tzinfo=CN_TZ)  # 本地副本过期且上游失败
    state.fake = _Fake(ITEMS, org_fail=True)
    run(date(2026, 11, 10), ["600519.SH"])
    assert na._sync_status()["state"] == "ready"
    (data_dir / "news_announcements" / "_org_ids.json").unlink()
    run(date(2026, 11, 10), ["000001.SZ"])
    assert na._sync_status() == {"state": "error", "message": "巨潮暂时无法访问，请稍后重试。",
                                 "done": 0, "total": 1, "failed": 0}


def test_partial_coverage_is_reported(env):
    data_dir, state, run = env
    state.fake = _Fake(ITEMS)
    run(date(2026, 11, 9), ["300033.SZ"])
    result = na.get_risks(data_dir, date(2026, 11, 10), ["300033.SZ"])
    assert result["partial"] == ["300033.SZ"]


def test_routes(env, monkeypatch):
    data_dir, state, _ = env
    state.fake = _Fake(ITEMS)
    monkeypatch.setattr(extension, "cn_today", lambda: date(2026, 11, 11))
    monkeypatch.setattr(extension.watchlist, "list_symbols", lambda: [{"symbol": "300033.SZ"}])
    app = FastAPI()
    app.include_router(extension.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))
    client = TestClient(app)
    assert client.get("/api/news/announcements", params={"date": "2026-11-12"}).status_code == 422
    assert client.post("/api/news/announcements/sync", json={"date": "2026-11-12"}).status_code == 422
    started = client.post("/api/news/announcements/sync", json={"date": "2026-11-10"})
    assert started.status_code == 202 and started.json()["started"] is True
    _wait()
    body = client.get("/api/news/announcements", params={"date": "2026-11-10"}).json()
    assert body["symbols"] == 1 and [r["id"] for r in body["rows"]] == ["a1", "a2"]
