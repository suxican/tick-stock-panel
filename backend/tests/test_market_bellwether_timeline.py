"""盘中风向标状态时间线: 状态变化记录、计数与方向、按日保存与重启读回、后台记录器节拍。"""
import json
from datetime import datetime, timedelta

import pytest

from app.market_time import CN_TZ
from app.services import market_bellwether_timeline as tl

DAY = datetime(2026, 11, 12, 9, 30, tzinfo=CN_TZ)
DRAGON, CORE, MINE = "600001.SH", "600003.SH", "600101.SH"


def at(hh: int, mm: int, ss: int = 0, day: datetime = DAY) -> datetime:
    return day.replace(hour=hh, minute=mm, second=ss)


def market(state: str, verdict: str = "attack", *, as_of="2026-11-12", live=True, stale=False, status="ready"):
    return {"scope": "market", "status": status, "as_of": as_of, "live": live, "stale": stale,
            "verdict": {"key": verdict, "label": verdict},
            "items": [{"symbol": DRAGON, "name": "龙头", "state": state, "roles": [{"key": "emotion"}]}]}


def holdings(core_state: str, mine_state: str, verdict: str = "neutral"):
    return {"scope": "holdings", "status": "ready", "as_of": "2026-11-12", "live": True, "stale": False,
            "sectors": [{"kind": "concept", "name": "机器人", "verdict": {"key": verdict},
                         "items": [{"symbol": CORE, "name": "中军股", "state": core_state, "roles": []}],
                         "mine": [{"symbol": MINE, "name": "我的股", "state": mine_state}]}]}


def test_records_transitions_counts_and_first_seal(tmp_path):
    timeline = tl.BellwetherTimeline(tmp_path)
    for moment, state in ((at(9, 31), "strong"), (at(9, 35), "strong"), (at(10, 12), "sealed"),
                          (at(10, 15), "broken"), (at(10, 20), "sealed"), (at(10, 40), "unknown"),
                          (at(11, 0), "broken")):
        timeline.observe(moment, [market(state)])
    summary = timeline.stock_summary(DRAGON, at(11, 5))
    assert summary["first_sealed_at"] == "10:12:00"
    assert (summary["broken_count"], summary["resealed_count"]) == (2, 1)
    # 首次观测只记开盘状态, 同一状态不重复, 待确认不记
    assert [(e["from"], e["to"]) for e in summary["events"]] == [
        (None, "strong"), ("strong", "sealed"), ("sealed", "broken"), ("broken", "sealed"), ("sealed", "broken")]


@pytest.mark.parametrize("states,trend", [
    ([(9, 31, "weak"), (10, 50, "sealed")], "up"),        # 15 分钟前走弱, 现在封板
    ([(9, 31, "sealed"), (10, 55, "broken")], "down"),
    ([(9, 31, "strong"), (10, 59, "sealed")], None),       # 同为强势一档, 不算转向
    ([(10, 55, "strong")], None),                          # 15 分钟前尚无记录
])
def test_trend_compares_with_fifteen_minutes_ago(tmp_path, states, trend):
    timeline = tl.BellwetherTimeline(tmp_path)
    for hh, mm, state in states:
        timeline.observe(at(hh, mm), [market(state)])
    assert timeline.stock_summary(DRAGON, at(11, 0))["trend"] == trend


def test_sector_and_market_verdict_changes(tmp_path):
    timeline = tl.BellwetherTimeline(tmp_path)
    timeline.observe(at(9, 31), [market("strong", "attack"), holdings("strong", "flat", "neutral")])
    timeline.observe(at(10, 35), [market("strong", "divergence"), holdings("weak", "weak", "retreat")])
    snap = timeline.annotate(holdings("weak", "weak", "retreat"), at(10, 36))
    sector = snap["sectors"][0]
    assert sector["verdict_change"] == {"t": "10:35:00", "from": "neutral", "to": "retreat"}
    assert sector["items"][0]["timeline"]["events"][-1]["to"] == "weak"
    assert sector["mine"][0]["timeline"]["events"][-1] == {"t": "10:35:00", "from": "flat", "to": "weak"}
    assert timeline.annotate(market("strong", "divergence"), at(10, 36))["verdict_change"]["to"] == "divergence"


def test_ignores_stale_unready_other_day_and_offline_snapshots(tmp_path):
    timeline = tl.BellwetherTimeline(tmp_path)
    for snap in (market("sealed", stale=True), market("sealed", status="unavailable"),
                 market("sealed", as_of="2026-11-11"), market("sealed", live=False)):
        assert timeline.observe(at(10, 0), [snap]) is False
    assert timeline.stock_summary(DRAGON, at(10, 1)) is None
    # 其他交易日的快照不附加今天的时间线
    assert "timeline" not in timeline.annotate(market("sealed", as_of="2026-11-11"), at(10, 1))["items"][0]


def test_annotate_does_not_mutate_the_cached_snapshot(tmp_path):
    timeline = tl.BellwetherTimeline(tmp_path)
    cached = market("sealed")
    timeline.observe(at(10, 0), [cached])
    annotated = timeline.annotate(cached, at(10, 1))
    assert annotated["items"][0]["timeline"]["first_sealed_at"] == "10:00:00"
    assert "timeline" not in cached["items"][0]


def test_persists_per_day_restores_after_restart_and_keeps_five_days(tmp_path):
    folder = tmp_path / "user_data" / "bellwether_timeline"
    folder.mkdir(parents=True)
    for i in range(6):
        (folder / f"2026-11-0{i + 1}.json").write_text("{}", encoding="utf-8")
    (folder / "keep.txt").write_text("not ours", encoding="utf-8")
    timeline = tl.BellwetherTimeline(tmp_path)
    timeline.observe(at(10, 0), [market("strong")])
    timeline.observe(at(10, 5), [market("sealed")])
    saved = json.loads((folder / "2026-11-12.json").read_text(encoding="utf-8"))
    assert saved["version"] == 1 and saved["stocks"][DRAGON]["state"] == "sealed"
    assert sorted(p.name for p in folder.iterdir()) == [
        "2026-11-03.json", "2026-11-04.json", "2026-11-05.json", "2026-11-06.json", "2026-11-12.json", "keep.txt"]

    restarted = tl.BellwetherTimeline(tmp_path)
    restarted.observe(at(10, 30), [market("broken")])
    summary = restarted.stock_summary(DRAGON, at(10, 31))
    assert summary["first_sealed_at"] == "10:05:00" and summary["broken_count"] == 1


def test_corrupt_file_starts_fresh_and_new_day_resets(tmp_path):
    folder = tmp_path / "user_data" / "bellwether_timeline"
    folder.mkdir(parents=True)
    (folder / "2026-11-12.json").write_text("{broken", encoding="utf-8")
    timeline = tl.BellwetherTimeline(tmp_path)
    timeline.observe(at(10, 0), [market("sealed")])
    assert timeline.stock_summary(DRAGON, at(10, 1))["first_sealed_at"] == "10:00:00"
    next_day = DAY + timedelta(days=1)
    snap = market("strong", as_of="2026-11-13")
    timeline.observe(at(9, 31, day=next_day), [snap])
    summary = timeline.stock_summary(DRAGON, at(9, 32, day=next_day))
    assert summary["first_sealed_at"] is None and len(summary["events"]) == 1


# ---------------------------------------------------------------- 后台记录器
class FakeQuotes:
    def __init__(self):
        self.subscribers = []

    def subscribe(self):
        sub = type("Sub", (), {"wait": lambda self, timeout=5.0: True, "pop": lambda self: {}})()
        self.subscribers.append(sub)
        return sub

    def unsubscribe(self, sub):
        self.subscribers.remove(sub)


def test_tracker_ticks_only_in_session_and_throttles(tmp_path):
    calls = []

    def snapshot_fn(scope):
        calls.append(scope)
        return market("sealed") if scope == "market" else {"scope": scope, "status": "unavailable"}

    tracker = tl.BellwetherTracker(FakeQuotes(), tl.BellwetherTimeline(tmp_path), snapshot_fn=snapshot_fn)
    assert tracker.tick(at(9, 20)) is False and calls == []          # 集合竞价不记录
    assert tracker.tick(at(12, 0)) is False and calls == []          # 午休不记录
    assert tracker.tick(at(10, 0)) is True and calls == ["market", "holdings", "watchlist"]
    assert tracker.tick(at(10, 0, 3)) is False and len(calls) == 3   # 5 秒内不重复
    assert tracker.tick(at(10, 0, 6)) is True


def test_tracker_survives_snapshot_errors(tmp_path):
    def snapshot_fn(scope):
        if scope == "holdings":
            raise RuntimeError("boom")
        return market("sealed") if scope == "market" else {"scope": scope, "status": "unavailable"}

    timeline = tl.BellwetherTimeline(tmp_path)
    tracker = tl.BellwetherTracker(FakeQuotes(), timeline, snapshot_fn=snapshot_fn)
    assert tracker.tick(at(10, 0)) is True
    assert timeline.stock_summary(DRAGON, at(10, 1))["first_sealed_at"] == "10:00:00"


def test_tracker_thread_starts_and_stops_cleanly(tmp_path):
    quotes = FakeQuotes()
    tracker = tl.BellwetherTracker(quotes, tl.BellwetherTimeline(tmp_path),
                                   snapshot_fn=lambda scope: {"status": "unavailable"})
    tracker.start()
    tracker.stop()
    assert quotes.subscribers == [] and not tracker.running


def test_api_attaches_timeline_only_when_tracker_runs(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.market_recap import router
    from app.services import market_bellwether

    monkeypatch.setattr(tl, "cn_now", lambda: at(10, 1))
    monkeypatch.setattr(market_bellwether, "get_snapshot", lambda repo, **kw: market("sealed"))
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    client = TestClient(app)
    assert "timeline" not in client.get("/api/market-recap/bellwethers").json()["items"][0]
    timeline = tl.BellwetherTimeline(tmp_path)
    timeline.observe(at(10, 0), [market("sealed")])
    app.state.bellwether_tracker = SimpleNamespace(timeline=timeline)
    item = client.get("/api/market-recap/bellwethers").json()["items"][0]
    assert item["timeline"]["first_sealed_at"] == "10:00:00"
