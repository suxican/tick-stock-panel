"""资讯分析·催化验证服务测试 (不依赖真实网络)。

覆盖: 上游记录规范化 (市场过滤/归属日/代码解析/去重)、关键词聚合与盘面核对的数值口径、
新鲜度与回看完整性、次日延续、按日缓存与分段拉取、数据源缺失/失败/截断的降级。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom import news as extension
from app.market_time import CN_TZ
from app.plugins.fuyao.client import FuyaoError
from app.services import news_catalysts as svc

TODAY = date(2026, 11, 11)
DAYS = ["2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10"]
NAMES = {"600001.SH": "甲", "000002.SZ": "乙", "300003.SZ": "丙", "920004.BJ": "丁"}


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=CN_TZ).timestamp() * 1000)


def _raw(code: str, day: str, keywords: list[str], *, tag: str = "LIMIT_UP", at: str = "09:40:00",
         domain: str = "A_SHARES", content: str = "行业原因") -> dict:
    return {
        "analysis_id": f"{code}-{day}-{at}", "domain": domain, "analysis_content": content,
        "key_word_list": keywords, "tag_code": tag, "tag_name": "涨停" if tag == "LIMIT_UP" else "大涨",
        "link_url": f"https://example.invalid/app/?marketId=33&thsHqCode={code}&date={day}",
        "last_anomaly_time": _ms(f"{day}T{at}"), "first_publish_time": _ms(f"{day}T{at}") + 60_000,
    }


class _Provider:
    def __init__(self, items: list[dict], *, fail: bool = False, truncated: bool = False,
                 single_day_only: bool = False):
        self.items, self.fail, self.truncated = items, fail, truncated
        self.single_day_only = single_day_only
        self.calls: list[tuple[str, str]] = []

    def anomaly_analysis(self, begin: str, end: str) -> dict:
        self.calls.append((begin, end))
        if self.fail or (self.single_day_only and begin != end):
            raise FuyaoError("code=5003: internal upstream detail")
        return {"items": self.items, "was_truncated": self.truncated}


def _q(symbol: str, change: float, *, limit_up: bool = False, amount: float = 1e8) -> dict:
    return {"symbol": symbol, "change": change, "limit_up": limit_up, "limit_down": False, "amount": amount}


QUOTES = {
    date(2026, 11, 10): {
        "600001": _q("600001.SH", 0.1, limit_up=True, amount=3e8),
        "000002": _q("000002.SZ", 0.1, limit_up=True, amount=9e8),
        "300003": _q("300003.SZ", 0.04, amount=5e8),
    },
    date(2026, 11, 9): {
        "600001": _q("600001.SH", 0.1, limit_up=True),
        "000002": _q("000002.SZ", 0.02),
    },
}


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    for day in DAYS:
        (tmp_path / "kline_daily" / f"date={day}").mkdir(parents=True)
    state = SimpleNamespace(provider=None, quotes=QUOTES)
    monkeypatch.setattr(svc, "cn_today", lambda: TODAY)
    monkeypatch.setattr(svc, "cn_now", lambda: datetime(2026, 11, 11, 8, 0, tzinfo=CN_TZ))
    monkeypatch.setattr(svc, "_provider", lambda: state.provider)
    monkeypatch.setattr(svc, "_quotes", lambda repo, d: (state.quotes.get(d, {}), None))
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_name_map=lambda symbols: {s: NAMES[s] for s in symbols if s in NAMES},
    )
    return repo, state, tmp_path


def _full_history() -> list[dict]:
    rows = [
        _raw("600001", "2026-11-10", ["机器人", "减速器"]),
        _raw("000002", "2026-11-10", ["机器人"]),
        _raw("300003", "2026-11-10", ["机器人", "减速器"], tag="SHARP_RISE"),
        _raw("920004", "2026-11-10", ["固态电池"]),
        _raw("600001", "2026-11-09", ["减速器"]),
    ]
    # 其余回看日各放一条无关记录, 使回看窗口完整。
    return rows + [_raw("920004", day, ["固态电池"]) for day in DAYS[:3]]


def test_normalize_filters_market_day_and_code_and_keeps_latest():
    day = date(2026, 11, 10)
    raw = [
        _raw("600001", "2026-11-10", ["A", "A", " B "], at="09:31:00", content="早"),
        _raw("600001", "2026-11-10", ["C"], at="14:02:00", content="晚"),
        _raw("000002", "2026-11-10", ["A"], domain="US_STOCKS"),
        _raw("000002", "2026-11-09", ["A"]),
        _raw("900901", "2026-11-10", ["A"]),
        _raw("300003", "2026-11-10", ["A"], content="  "),
        {"domain": "A_SHARES", "link_url": None},
        "bad",
    ]
    rows = svc.normalize_items(raw, {day})[day]
    assert [r["symbol"] for r in rows] == ["600001.SH"]
    assert rows[0]["content"] == "晚" and rows[0]["keywords"] == ["C"]
    assert rows[0]["anomaly_at"] == "2026-11-10T14:02:00+08:00"
    assert rows[0]["published_at"] == "2026-11-10T14:03:00+08:00"
    early = svc.normalize_items(raw[:1], {day})[day][0]
    assert early["keywords"] == ["A", "B"]


def test_groups_verify_breadth_freshness_and_next_day(env):
    repo, state, _ = env
    state.provider = _Provider(_full_history())
    result = svc.get_catalysts(repo, date(2026, 11, 9))
    assert result["state"] == "ok" and result["trade_date"] == "2026-11-09"
    assert result["groups"] == []  # 单只个股的关键词不成组
    assert result["next_date"] == "2026-11-10"
    assert result["items"][0]["next_change_pct"] == pytest.approx(0.1)

    result = svc.get_catalysts(repo, date(2026, 11, 10))
    assert result["live"] is False and result["quote_state"] == "ok"
    assert result["lookback"] == {"days": 4, "cached": 4}
    assert result["counts"] == {"stocks": 4, "limit_up": 3}
    assert result["next_date"] is None  # 11-11 尚无本地分区
    robot, reducer = result["groups"]
    assert robot["keyword"] == "机器人" and robot["symbols"] == ["000002.SZ", "600001.SH", "300003.SZ"]
    assert robot["stock_count"] == 3 and robot["limit_up_count"] == 2 and robot["batch"] is True
    assert robot["avg_change_pct"] == pytest.approx(0.08)  # 小数制: (0.1 + 0.1 + 0.04) / 3
    assert robot["leader"]["symbol"] == "000002.SZ"  # 同涨幅取成交额大者
    assert robot["anchor"] == {"symbol": "000002.SZ", "name": "乙", "change_pct": 0.1, "amount": 9e8}
    assert robot["fresh"] is True and robot["seen_dates"] == []
    assert robot["first_published_at"] == "2026-11-10T09:41:00+08:00"
    assert robot["next_day"] is None
    assert reducer["keyword"] == "减速器" and reducer["batch"] is False
    assert reducer["fresh"] is False and reducer["seen_dates"] == ["2026-11-09"]
    assert [i["symbol"] for i in result["items"]][-1] == "920004.BJ"  # 无行情排最后
    assert result["items"][-1]["change_pct"] is None and result["items"][-1]["name"] == "丁"


def test_history_is_cached_and_fetched_in_bounded_spans(env):
    repo, state, data_dir = env
    state.provider = _Provider(_full_history())
    first = svc.get_catalysts(repo, date(2026, 11, 10))
    assert state.provider.calls == [("2026-11-04", "2026-11-06"), ("2026-11-09", "2026-11-10")]
    saved = json.loads((data_dir / "news_catalysts" / "date=2026-11-10.json").read_text(encoding="utf-8"))
    assert saved["date"] == "2026-11-10" and len(saved["items"]) == 4
    state.provider = None  # 数据源下线后历史仍可读
    assert svc.get_catalysts(repo, date(2026, 11, 10)) == first


def test_oversized_span_is_split_down_to_single_days(env):
    repo, state, _ = env
    state.provider = _Provider(_full_history(), single_day_only=True)
    result = svc.get_catalysts(repo, date(2026, 11, 10))
    assert result["state"] == "ok" and result["lookback"] == {"days": 4, "cached": 4}
    assert [c for c in state.provider.calls if c[0] == c[1]] == [(d, d) for d in DAYS]


def test_today_is_fetched_live_and_not_cached(env, monkeypatch):
    repo, state, data_dir = env
    monkeypatch.setattr(svc, "cn_today", lambda: date(2026, 11, 10))
    state.provider = _Provider(_full_history())
    assert svc.get_catalysts(repo, None)["live"] is True
    assert not (data_dir / "news_catalysts" / "date=2026-11-10.json").exists()
    assert (data_dir / "news_catalysts" / "date=2026-11-09.json").exists()


def test_today_without_records_yet_shows_previous_session(env):
    repo, state, _ = env
    state.provider = _Provider(_full_history())  # 11-11 开盘前, 上游尚无当日记录
    result = svc.get_catalysts(repo, None)
    assert result["state"] == "ok" and result["live"] is False
    assert result["requested_date"] == "2026-11-11" and result["trade_date"] == "2026-11-10"


def test_incomplete_lookback_does_not_claim_first_appearance(env):
    repo, state, data_dir = env
    state.provider = _Provider([r for r in _full_history() if "2026-11-10" in r["analysis_id"]])
    result = svc.get_catalysts(repo, date(2026, 11, 10))
    assert result["lookback"] == {"days": 4, "cached": 0}
    assert {g["keyword"]: g["fresh"] for g in result["groups"]} == {"机器人": None, "减速器": None}
    assert not (data_dir / "news_catalysts" / "date=2026-11-06.json").exists()  # 空结果不落缓存


def test_missing_quotes_fall_back_to_source_tags(env):
    repo, state, _ = env
    state.provider = _Provider(_full_history())
    state.quotes = {}
    result = svc.get_catalysts(repo, date(2026, 11, 10))
    robot = result["groups"][0]
    assert result["quote_state"] == "unavailable"
    assert robot["limit_up_count"] == 2 and robot["avg_change_pct"] is None
    assert robot["leader"] is None and robot["anchor"] is None and robot["batch"] is True


def test_source_missing_failed_or_truncated_fails_closed(env):
    repo, state, data_dir = env
    assert svc.get_catalysts(repo, date(2026, 11, 10))["state"] == "source_unavailable"
    state.provider = _Provider(_full_history(), fail=True)
    failed = svc.get_catalysts(repo, date(2026, 11, 10))
    assert failed["state"] == "no_data" and "upstream" not in failed["message"]
    state.provider = _Provider(_full_history(), truncated=True)
    assert svc.get_catalysts(repo, date(2026, 11, 10))["state"] == "no_data"
    assert not (data_dir / "news_catalysts").exists()


def test_non_trading_day_resolves_to_previous_session(env):
    repo, state, _ = env
    state.provider = _Provider(_full_history())
    result = svc.get_catalysts(repo, date(2026, 11, 8))
    assert result["requested_date"] == "2026-11-08" and result["trade_date"] == "2026-11-06"


def test_route_rejects_future_and_returns_service_payload(env, monkeypatch):
    repo, state, _ = env
    state.provider = _Provider(_full_history())
    monkeypatch.setattr(extension, "cn_today", lambda: TODAY)
    app = FastAPI()
    app.include_router(extension.router)
    app.state.repo = repo
    client = TestClient(app)
    assert client.get("/api/news/catalysts", params={"date": "2026-11-12"}).status_code == 422
    assert client.get("/api/news/catalysts", params={"date": "bad"}).status_code == 422
    body = client.get("/api/news/catalysts", params={"date": "2026-11-10"}).json()
    assert body["state"] == "ok" and body["groups"][0]["keyword"] == "机器人"
