from datetime import datetime, timedelta
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

from app.market_time import CN_TZ
from app.services import first_board_radar as radar

NOW = datetime(2026, 11, 5, 10, 0, tzinfo=CN_TZ)
S = "000001.SZ"


def hot(now, rank=5):
    return {"kind": "ths_hot_top", "date": now.date().isoformat(), "stale": False,
            "fetched_at": now.isoformat(), "rows": [{"trade_date": now.date().isoformat(),
            "symbol": S, "rank": rank, "updated_at": now.isoformat()}]}


def test_rank_uses_two_distinct_observations_and_restores(tmp_path):
    t2 = NOW + timedelta(minutes=5)
    views = radar._hot_history(tmp_path, hot(NOW, 19), NOW)
    assert radar._rank_rows(views, NOW)[0][S]["rank_gain"] is None
    assert not radar._rank_rows(views, NOW - timedelta(seconds=1))[0]
    assert not radar._rank_rows(views, NOW + timedelta(minutes=11))[0]
    views = radar._hot_history(tmp_path, hot(t2, 5), t2)
    assert radar._rank_rows(views, t2)[0][S]["rank_gain"] == 14
    duplicate = radar._hot_history(tmp_path, hot(t2, 5), t2 + timedelta(seconds=2))
    assert radar._rank_rows(duplicate, t2 + timedelta(seconds=2))[0][S]["prior_rank"] == 19
    assert not radar._hot_history(tmp_path, hot(NOW, 19), t2 + timedelta(seconds=4))
    assert not radar._rank_rows(views, t2)[1]  # partial Top 100 cannot prove an exclusion


def test_cross_day_source_late_or_stale_never_saved(tmp_path):
    for stamp in (NOW + timedelta(days=1), NOW + timedelta(seconds=1), NOW - timedelta(days=1)):
        packet = hot(NOW)
        packet["rows"][0]["updated_at"] = stamp.isoformat()
        assert not radar._hot_history(tmp_path, packet, NOW)
    packet = dict(hot(NOW), stale=True)
    assert not radar._hot_history(tmp_path, packet, NOW)
    assert not list(tmp_path.rglob("*.json"))


def test_disabled_empty_and_async_provider_dont_block(tmp_path, monkeypatch):
    started, release = Event(), Event()
    monkeypatch.setattr(radar, "cn_now", lambda: NOW)
    def wait(kind, day, **kwargs):
        started.set()
        release.wait(2)
        return hot(NOW)
    monkeypatch.setattr(radar.shared_reader, "read", wait)
    svc = radar.FirstBoardRadar(tmp_path)
    assert svc.snapshot({S}, now=NOW, enabled=False) == {}
    assert svc.snapshot(set(), now=NOW, enabled=True) == {}
    assert not started.is_set()
    assert svc.snapshot({S}, now=NOW, enabled=True)[S]["popularity"]["status"] == "unavailable"
    assert started.wait(2)
    assert svc.snapshot({S}, now=NOW, enabled=True)[S]["popularity"]["status"] == "unavailable"
    release.set()
    svc.stop()


def test_full_top100_absence_and_exact_expiry(tmp_path):
    packet = hot(NOW)
    row = packet["rows"][0]
    packet["rows"] = [dict(row, rank=n, symbol=f"{n:06d}.SZ") for n in range(1, 101)]
    views = radar._hot_history(tmp_path, packet, NOW)
    assert radar._rank_rows(views, NOW + timedelta(minutes=10))[1]
    assert not radar._rank_rows(views, NOW + timedelta(minutes=10, seconds=1))[1]
    packet["rows"][1]["rank"] = 1
    assert not radar._hot_history(tmp_path, packet, NOW)


def test_source_failure_isolated_from_exact_members_and_official_core(tmp_path, monkeypatch):
    day = NOW.date().isoformat()
    monkeypatch.setattr(radar, "cn_now", lambda: NOW)
    def read(kind, date, *, plate_type=None):
        if kind == "ths_hot_top":
            raise RuntimeError("offline")
        source_time = NOW - timedelta(minutes=1) if int(plate_type) == 17 else NOW
        row = {"trade_date": day, "plate_type": int(plate_type), "plate_code": "P1", "plate_name": "同名板块",
               "source_score": 77, "updated_at": source_time.isoformat(), "change_pct": 1.2}
        return {"date": day, "fetched_at": NOW.isoformat(), "stale": False, "rows": [row]}
    monkeypatch.setattr(radar.shared_reader, "read", read)
    def roster(kind, date, *, plate_type, code):
        stocks = {14: [S, "600002.SH"], 15: [S], 17: [S, "600003.SH"]}[plate_type]
        return [{"trade_date": day, "plate_type": plate_type, "plate_code": code, "symbol": s}
                for s in stocks]
    provider = Mock(side_effect=roster)
    monkeypatch.setattr(radar.custom_sources, "get_provider", lambda _: SimpleNamespace(research=provider))
    home = {"state": "ready", "mode": "intraday", "stale": False, "market_snapshot_at": "09:59",
            "source_at": NOW.isoformat(), "fetched_at": NOW.isoformat(),
            "rows": [{"tid": "12", "name": "官方方向", "score": 75, "rank": 2, "core_stocks": [{"symbol": S}]}]}
    def archive(root, day, *, as_of):
        assert day == NOW.date() and as_of == NOW
        return home
    monkeypatch.setattr(radar, "read_home", archive)
    svc = radar.FirstBoardRadar(tmp_path)
    svc._refresh(NOW)
    svc._next = NOW + timedelta(minutes=5)
    got = svc.snapshot({S, "600002.SH", "600004.SH"}, now=NOW, enabled=True)
    assert got[S]["popularity"]["status"] == "unavailable"
    assert got[S]["theme"]["source"] == "official_core"
    assert got[S]["plate"]["plate_type"] == 17
    assert [p["plate_type"] for p in got[S]["plates"]] == [17, 15, 14]
    assert [p["plate_type"] for p in got["600002.SH"]["plates"]] == [14]
    assert got["600002.SH"]["theme"]["plate_type"] == 14
    assert got["600004.SH"]["theme"]["status"] == "unavailable"
    aged = svc.snapshot({S}, now=NOW + timedelta(minutes=10), enabled=True)[S]
    assert [p["plate_type"] for p in aged["plates"]] == [15, 14]
    assert aged["plate"]["plate_type"] == 15
    svc._refresh(NOW)
    assert provider.call_count == 3  # one dated complete roster per type/plate for the day
    home["market_snapshot_at"] = "09:35"
    assert radar._official(tmp_path, NOW) == {}
    later = NOW + timedelta(minutes=11)
    assert svc.snapshot({S}, now=later, enabled=True)[S]["plate"]["status"] == "unavailable"
    svc.stop()


def test_auction_cutoff_blocks_new_fetch_inflight_rank_and_archive(tmp_path, monkeypatch):
    svc = radar.FirstBoardRadar(tmp_path)
    read = Mock(side_effect=AssertionError("unexpected provider call"))
    monkeypatch.setattr(radar.shared_reader, "read", read)
    for h, m in ((11, 30), (12, 59), (14, 57), (15, 0)):
        clock = NOW.replace(hour=h, minute=m)
        monkeypatch.setattr(radar, "cn_now", lambda c=clock: c)
        assert svc.snapshot({S}, now=clock, enabled=True) == {}
        before = clock - timedelta(seconds=1)
        svc._refresh(before)
        assert not radar._hot_history(tmp_path, hot(before), clock)
    assert not read.called
    svc.stop()
