import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import pytest

from app.services import zzshare_home as home

D = date(2026, 11, 4)


def ts(day, hour, minute=0):
    return datetime.fromisoformat(f"{day}T{hour:02}:{minute:02}:00+08:00")


def packet(day, tids, *, live=False, source_at=None):
    return {
        "date": day, "source": "realtime" if live else "historical_score",
        "cache_status": "live" if live else "historical",
        "source_asof": source_at or ts(day, 10 if live else 15).isoformat(),
        "market_snapshot_at": "10:00" if live else "15:00", "is_trading": live,
        "candidate_count": 66, "strategy_candidate_count": 5, "selection_rule": "官方规则",
        "items": [{"tid": i, "name": "题材" + i, "score": 90-j} for j, i in enumerate(tids)],
    }


@pytest.fixture
def rig(tmp_path, monkeypatch):
    class Source:
        def __init__(self):
            self.calls = []
            self.now = ts(D, 10)
            self.payloads = {
                None: packet(str(D), ["C","A","N"], live=True),
                str(D): packet(str(D), ["N","A","C"]),
                "2026-11-03": packet("2026-11-03", ["A","B","C"]),
                "2026-11-02": packet("2026-11-02", ["A","X","Y"]),
                "2026-10-30": packet("2026-10-30", ["Z","X","Y"]),
            }

        def home_topics(self, day):
            self.calls.append(day)
            return self.payloads[day]

        def home_topic_dates(self):
            return sorted((k for k in self.payloads if k), reverse=True)
    src = Source()
    monkeypatch.setattr(home, "cn_now", lambda: src.now)
    monkeypatch.setattr(home.custom_sources, "get_provider", lambda _: src)
    return tmp_path, src


def test_first_read_syncs_official_order_and_freezes_known_metrics(rig):
    root, src = rig
    result = home.read_home(root, D)
    assert result["state"] == "ready" and result["mode"] == "intraday"
    assert str(D) not in src.calls
    assert [r["tid"] for r in result["rows"]] == ["C","A","N"]
    assert [(r["rank_change"], r["new_entry"], r["streak"]) for r in result["rows"]] == [
        (2, False, 2), (-1, False, 3), (None, True, 1),
    ]
    assert result["points"] == [result["fetched_at"]] and result["source_at"] == src.payloads[None]["source_asof"]
    assert home.read_home(root, D)["points"] == result["points"]
    assert src.calls.count(None) == 1
    src.now = ts(D, 18)
    close = home.read_home(root, D)
    assert close["mode"] == "close" and close["rows"][0]["tid"] == "N"
    assert close["points"] == [*result["points"], close["fetched_at"]]
    before = list(src.calls)
    replay = home.read_home(root, D, as_of=ts(D, 10, 1))
    assert replay["rows"] == result["rows"] and replay["fetched_at"] == result["fetched_at"]
    assert home.read_home(root, D, as_of=ts(D, 9, 59))["state"] == "no_data"
    assert src.calls == before


def test_official_single_seat_close_confirms_absence_but_missing_day_is_unknown(rig):
    root, src = rig
    src.payloads["2026-11-03"] = packet("2026-11-03", ["A"])
    del src.payloads["2026-11-02"]
    rows = home.read_home(root,D)["rows"]
    assert rows[0]["new_entry"] is True and rows[0]["streak"] == 1
    assert rows[1]["streak_min"] == 2 and rows[1]["streak"] is None
    assert "2026-10-30" not in src.calls


def test_postclose_archive_is_not_an_intraday_observation(rig):
    root, src = rig
    src.now = ts(D, 18)
    snap = home.read_home(root, D)
    assert snap["mode"] == "close" and snap["points"] == [snap["fetched_at"]]
    assert home.read_home(root, D, as_of=ts(D, 14, 59))["rows"] == []
    assert home.read_home(root, D, as_of=ts(D, 15))["rows"] == []
    assert home.read_home(root, D, as_of=ts(D, 18))["rows"] == snap["rows"]
    assert None not in src.calls
    with pytest.raises(ValueError):
        home.read_home(root, D, as_of=datetime(2026, 11, 4, 12))
    with pytest.raises(ValueError):
        home.read_home(root, D, as_of=ts(D, 19))


def test_simultaneous_reads_share_one_capture(rig, monkeypatch):
    root, src = rig
    entered, release = threading.Event(), threading.Event()
    fetch = src.home_topics
    def slow(day):
        if day is None:
            entered.set()
            assert release.wait(5)
        return fetch(day)
    monkeypatch.setattr(src, "home_topics", slow)
    with ThreadPoolExecutor(max_workers=3) as pool:
        calls = [pool.submit(home.read_home, root, D) for _ in range(3)]
        assert entered.wait(5)
        release.set()
        rows = [f.result(timeout=10) for f in calls]
    assert rows[0] == rows[1] == rows[2] and src.calls.count(None) == 1


def test_confirmed_empty_close_absence_vs_unpublished_placeholder(rig):
    root, src = rig
    src.payloads["2026-11-03"] = packet("2026-11-03", [])
    result = home.read_home(root, D)
    assert all(r["new_entry"] and r["streak"] == 1 for r in result["rows"])
    src.now = ts(D, 18)
    src.payloads[str(D)] = dict(packet(str(D), []), source_asof=None, candidate_count=0)
    unavailable = home.read_home(root, D)
    assert unavailable["stale"] and unavailable["mode"] == "intraday"
    assert unavailable["points"] == result["points"]


@pytest.mark.parametrize("bad", [{"source_asof": None}, {"market_snapshot_at": None}, {"market_snapshot_at": "15:00"}])
def test_live_empty_placeholders_cannot_replace_prior_record(rig, monkeypatch, bad):
    root, src = rig
    tick = [0]
    monkeypatch.setattr(home, "monotonic", lambda: tick[0])
    first = home.read_home(root, D)
    tick[0] = 70
    src.now = ts(D, 10, 2)
    src.payloads[None] = {**packet(str(D), [], live=True), **bad}
    new = home.read_home(root, D)
    assert new["stale"] and new["rows"] == first["rows"] and new["points"] == first["points"]


def test_backfill_and_replay_derive_no_earlier_observation(rig, monkeypatch):
    root, src = rig
    tick = [0]
    monkeypatch.setattr(home, "monotonic", lambda: tick[0])
    src.payloads["2026-11-03"] = dict(packet("2026-11-03", ["C"]), source_asof=None)
    first = home.read_home(root, D)
    assert all(r["streak"] is None and r["new_entry"] is None for r in first["rows"])
    tick[0] = 90
    src.now = ts(D, 10, 4)
    src.payloads["2026-11-03"] = packet("2026-11-03", ["A", "B", "C"])
    second = home.read_home(root, D)
    assert second["rows"][0]["rank_change"] == 2 and len(second["points"]) == 2
    calls = list(src.calls)
    replay = home.read_home(root, D, as_of=ts(D, 10, 1))
    assert replay["rows"] == first["rows"] and src.calls == calls
    old = home.read_home(root, date(2026, 11, 3))
    assert old["mode"] == "close" and not old["points"]


def test_live_dedup_write_failure_and_corrupt_replay(rig, monkeypatch):
    root, src = rig
    tick = [0]
    monkeypatch.setattr(home, "monotonic", lambda: tick[0])
    first = home.read_home(root, D)
    src.now, tick[0] = ts(D, 10, 2), 90
    assert home.read_home(root, D)["points"] == first["points"]
    src.now, tick[0] = ts(D, 10, 4), 190
    src.payloads[None] = packet(str(D), ["N"], live=True)
    def fail(*_):
        raise OSError("readonly")
    monkeypatch.setattr(home, "_write", fail)
    stale = home.read_home(root, D)
    assert stale["stale"] and stale["rows"] == first["rows"]
    path = home._directory(root, D) / "observations.json"
    path.write_text("{", encoding="utf-8")
    error = home.read_home(root, D, as_of=ts(D, 10))
    assert error["state"] == "unavailable" and path.read_text(encoding="utf-8") == "{"


def test_comparison_is_bounded_and_streak_is_lower_bound(rig):
    root, src = rig
    from datetime import timedelta
    day = D
    for _ in range(13):
        day = day - timedelta(days=1)
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        src.payloads[str(day)] = packet(str(day), ["C", "A", "N"])
    snap = home.read_home(root, D)
    assert len(src.calls) == 11
    assert all(r["streak_min"] == 11 and r["streak"] is None for r in snap["rows"])


def test_rate_limit_applies_across_days_and_replay_never_contacts_upstream(rig, monkeypatch):
    from app.plugins.zzshare import ZzshareError
    root, src = rig
    tick = [0]
    monkeypatch.setattr(home, "monotonic", lambda: tick[0])
    first = home.read_home(root, D)
    tick[0] = 65
    attempts = []
    def limited(day):
        attempts.append(day)
        raise ZzshareError("secret", status_code=429)
    monkeypatch.setattr(src, "home_topics", limited)
    stale = home.read_home(root, D)
    assert stale["stale"] and stale["points"] == first["points"]
    old = home.read_home(root, date(2026, 10, 29))
    assert old["state"] == "unavailable" and "受限" in old["message"]
    assert attempts == [None]


def test_newer_official_close_can_update_daily_view_but_cannot_rewrite_old_point(rig, monkeypatch):
    root, src = rig
    tick = [0]
    monkeypatch.setattr(home, "monotonic", lambda: tick[0])
    src.now = ts(D, 16)
    old = home.read_home(root, D)
    src.payloads[str(D)] = packet(str(D), ["Z","A"], source_at=ts(D, 16, 2).isoformat())
    src.now, tick[0] = ts(D, 16, 3), 180
    assert home.read_home(root, D)["rows"] == old["rows"]
    assert src.calls.count(str(D)) == 1
    src.now, tick[0] = ts(D, 16, 6), 360
    revised = home.read_home(root, D)
    assert revised["rows"][0]["tid"] == "Z" and not revised["stale"]
    assert revised["points"] == [old["fetched_at"], revised["fetched_at"]]
    assert home.read_home(root, D, as_of=ts(D, 16, 1))["rows"] == old["rows"]
    src.payloads[str(D)] = packet(str(D), ["A"], source_at=ts(D, 16, 1).isoformat())
    src.now, tick[0] = ts(D, 16, 13), 800
    rollback = home.read_home(root, D)
    assert rollback["rows"] == revised["rows"] and rollback["stale"]
    assert rollback["points"] == revised["points"]


def test_historical_daily_reverification_and_offline_same_date_fallback(rig, monkeypatch):
    root, src = rig
    tick = [0]
    monkeypatch.setattr(home, "monotonic", lambda: tick[0])
    old_day = date(2026, 11, 3)
    first = home.read_home(root, old_day)
    count = src.calls.count(str(old_day))
    src.now, tick[0] = ts("2026-11-05", 9), 23 * 3600
    assert home.read_home(root, old_day)["rows"] == first["rows"]
    assert src.calls.count(str(old_day)) == count
    src.now, tick[0] = ts("2026-11-05", 12), 26 * 3600
    def offline(_):
        raise RuntimeError("private upstream error")
    monkeypatch.setattr(src, "home_topics", offline)
    stale = home.read_home(root, old_day)
    assert stale["stale"] and stale["rows"] == first["rows"]
    assert stale["fetched_at"] == first["fetched_at"] and not stale["points"]
    assert "已保存的同日正式日榜" in stale["message"] and "private" not in stale["message"]
