from __future__ import annotations

from datetime import datetime, timedelta

from app.market_time import CN_TZ
from app.services.first_board_exchange_limit import exchange_limit_evidence
from app.services.meoz_limit_stream import LimitState

NOW = datetime(2026, 11, 20, 10, 0, 0, tzinfo=CN_TZ)
AT = datetime(2026, 11, 20, 9, 45, 12, tzinfo=CN_TZ)
OK = {"state": "connected", "seq_gaps": 0}


def _limit(status, *, opens=0, pool=None, at=AT):
    return LimitState(status=status, at=at, touch_at=at, confirmed_opens=opens, pool_open_times=pool)


def _ev(quote, limit, stream=OK):
    return exchange_limit_evidence(quote, limit, stream, now=NOW)


def test_sealed_confirmed_note():
    r = _ev("sealed", _limit("sealed_confirmed"))
    assert (r["status"], r["conflict"], r["degraded"]) == ("sealed_confirmed", False, False)
    assert r["note"] == "交易所确认封板 09:45:12"
    assert r["at"] == AT.isoformat()


def test_sealed_confirmed_with_opens_note():
    r = _ev("sealed", _limit("sealed_confirmed", opens=2))
    assert r["note"] == "交易所确认封板 09:45:12，当日开板 2 次"
    assert r["confirmed_opens"] == 2


def test_sealed_touching_note():
    r = _ev("sealed", _limit("touching"))
    assert r["status"] == "touching"
    assert r["conflict"] is False
    assert r["note"] == "刚触及涨停，封板未满 60 秒"


def test_sealed_with_stale_opened_has_no_note_or_conflict():
    r = _ev("sealed", _limit("opened", opens=1))
    assert r["status"] == "opened"
    assert r["conflict"] is False
    assert r["note"] is None


def test_sealed_without_stream_event_is_not_conflict():
    r = _ev("sealed", None)
    assert r["status"] == "none"
    assert r["conflict"] is False
    assert r["note"] is None
    assert r["at"] is None


def test_sealed_with_none_status_is_not_conflict():
    r = _ev("sealed", _limit("none", at=None))
    assert (r["status"], r["conflict"], r["note"]) == ("none", False, None)


def test_broken_opened_note():
    r = _ev("broken", _limit("opened", opens=1))
    assert r["status"] == "opened"
    assert r["conflict"] is False
    assert r["note"] == "最近一次交易所确认开板 09:45:12，当日开板 1 次"


def test_broken_sealed_confirmed_fresh_is_conflict():
    r = _ev("broken", _limit("sealed_confirmed", at=NOW - timedelta(seconds=60)))
    assert r["conflict"] is True
    assert r["note"] is None


def test_broken_sealed_confirmed_stale_is_not_conflict():
    r = _ev("broken", _limit("sealed_confirmed", at=NOW - timedelta(seconds=61)))
    assert r["conflict"] is False
    assert r["note"] is None
    assert _ev("broken", _limit("sealed_confirmed", at=None))["conflict"] is False


def test_open_times_field_is_effective_count():
    assert _ev("broken", _limit("opened", opens=2, pool=0))["open_times"] == 2
    assert _ev("sealed", _limit("sealed_confirmed", opens=0, pool=None))["open_times"] == 0
    assert _ev("sealed", None)["open_times"] is None


def test_other_quote_state_has_no_note():
    for quote in ("none", "touching", "", None):
        r = _ev(quote, _limit("sealed_confirmed"))
        assert r["note"] is None
        assert r["conflict"] is False


def test_not_connected_is_unknown():
    for stream in ({"state": "idle", "seq_gaps": 0}, None, {}):
        r = _ev("sealed", _limit("sealed_confirmed"), stream)
        assert r["status"] == "unknown"
        assert r["note"] is None
        assert r["conflict"] is False


def test_seq_gap_keeps_facts_but_degraded():
    r = _ev("sealed", _limit("sealed_confirmed"), {"state": "connected", "seq_gaps": 2})
    assert r["status"] == "sealed_confirmed"
    assert r["degraded"] is True


def test_open_times_preferred_over_confirmed_opens():
    r = _ev("broken", _limit("opened", opens=1, pool=3))
    assert r["note"] == "最近一次交易所确认开板 09:45:12，当日开板 3 次"
    assert r["open_times"] == 3
    r2 = _ev("sealed", _limit("sealed_confirmed", opens=1, pool=3))
    assert r2["note"] == "交易所确认封板 09:45:12，当日开板 3 次"


def test_stale_pool_zero_is_topped_up_by_confirmed_opens():
    r = _ev("broken", _limit("opened", opens=1, pool=0))
    assert r["note"] == "最近一次交易所确认开板 09:45:12，当日开板 1 次"


def test_broken_opened_never_reports_zero_opens():
    r = _ev("broken", _limit("opened", opens=0, pool=0))
    assert r["note"] == "最近一次交易所确认开板 09:45:12，当日开板 1 次"


def test_missing_time_is_omitted_from_note():
    r = _ev("broken", _limit("opened", opens=1, at=None))
    assert r["note"] == "交易所确认开板，当日开板 1 次"
    r2 = _ev("sealed", _limit("sealed_confirmed", at=None))
    assert r2["note"] == "交易所确认封板"
    r3 = _ev("sealed", _limit("sealed_confirmed", opens=1, at=None))
    assert r3["note"] == "交易所确认封板，当日开板 1 次"
