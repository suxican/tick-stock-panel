"""Sentiment history dates, bounds, duplicate conflicts and snapshot isolation."""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.services import kaipanla
from app.services.kaipanla_catalog import normalize_emotion_history

DAY = date(2026, 9, 30)
HOLIDAY = date(2026, 10, 2)


@pytest.fixture(autouse=True)
def isolate_cache(monkeypatch):
    kaipanla._CACHE.clear()
    monkeypatch.setattr(kaipanla, "cn_today", lambda: HOLIDAY)
    yield
    kaipanla._CACHE.clear()


def _payload(rows):
    return {"errcode": "0", "info": rows}


@pytest.mark.parametrize("date_key", ["Date", "date", "Day", "day"])
def test_history_preserves_source_date_alias_and_compact_date(date_key):
    rows = normalize_emotion_history(_payload([
        {date_key: "20260929", "strong": "61", "ztjs": "57", "lbgd": "6", "df_num": "4"},
    ]), DAY)
    assert len(rows) == 1
    assert rows[0]["trade_date"] == "2026-09-29"
    assert rows[0]["strong"] == "61"
    assert "date" not in rows[0]


def test_identical_source_session_is_deduplicated_before_history_chart():
    row = {"Day": DAY.isoformat(), "strong": "56", "ztjs": "52"}
    rows = normalize_emotion_history(_payload([row, dict(row)]), DAY)
    assert len(rows) == 1 and rows[0]["trade_date"] == DAY.isoformat()


def test_conflicting_source_session_is_rejected_instead_of_last_write_wins():
    with pytest.raises(ValueError, match="冲突记录"):
        normalize_emotion_history(_payload([
            {"Day": DAY.isoformat(), "strong": "56", "ztjs": "52"},
            {"Day": DAY.isoformat(), "strong": "99", "ztjs": "52"},
        ]), DAY)


@pytest.mark.parametrize("row", [
    {"strong": "56"},
    {"Day": "2026-09-30", "Date": "2026-09-29", "strong": "56"},
    # A future row cannot conceal inconsistent date aliases.
    {"Day": "2026-10-02", "Date": "2026-10-01", "strong": "99"},
])
def test_history_requires_one_unambiguous_source_date(row):
    with pytest.raises(ValueError, match="日期"):
        normalize_emotion_history(_payload([row]), DAY)


def test_history_raw_row_limit_is_enforced_even_for_duplicate_dates():
    row = {"Day": DAY.isoformat(), "strong": "56"}
    assert len(normalize_emotion_history(_payload([row] * 1000), DAY)) == 1
    with pytest.raises(ValueError, match="数量"):
        normalize_emotion_history(_payload([row] * 1001), DAY)


def test_history_is_sorted_and_future_only_packet_is_empty():
    rows = normalize_emotion_history(_payload([
        {"Day": DAY.isoformat(), "strong": "56"},
        {"Day": HOLIDAY.isoformat(), "strong": "99"},
        {"Day": "2026-09-29", "strong": "61"},
    ]), DAY)
    assert [row["trade_date"] for row in rows] == ["2026-09-29", DAY.isoformat()]
    assert normalize_emotion_history(_payload([
        {"Day": HOLIDAY.isoformat(), "strong": "99"},
    ]), DAY) == []


@pytest.mark.parametrize("history_first", [True, False])
async def test_history_and_snapshot_have_separate_inflight_and_cached_results(monkeypatch, history_first):
    calls = []
    both_started = asyncio.Event()

    async def request(*args):
        calls.append(1)
        if len(calls) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=2)
        return _payload([
            {"Day": DAY.isoformat(), "strong": "56"},
            {"Day": "2026-09-29", "strong": "61"},
        ])

    monkeypatch.setattr(kaipanla, "_request", request)
    modes = [history_first, not history_first]
    results = await asyncio.gather(*(
        kaipanla.fetch_dataset("ext_kpl_emotion", DAY, include_history=mode) for mode in modes
    ))
    by_mode = dict(zip(modes, results, strict=True))
    assert len(calls) == 2
    assert "history" not in by_mode[False]
    assert len(by_mode[True]["history"]) == 2
    by_mode[True]["history"].clear()
    by_mode[True]["rows"][0]["strong"] = "999"
    history = await kaipanla.fetch_dataset("ext_kpl_emotion", DAY, include_history=True)
    snapshot = await kaipanla.fetch_dataset("ext_kpl_emotion", DAY)
    assert len(calls) == 2 and len(history["history"]) == 2
    assert history["rows"][0]["strong"] == snapshot["rows"][0]["strong"] == "56"


async def test_holiday_history_does_not_promote_request_date_to_response_date(monkeypatch):
    async def request(*args):
        return _payload([{"Day": DAY.isoformat(), "strong": "56", "ztjs": "52"}])

    monkeypatch.setattr(kaipanla, "_request", request)
    result = await kaipanla.fetch_dataset("ext_kpl_emotion", HOLIDAY, include_history=True)
    assert result["state"] == "empty" and result["rows"] == []
    assert result["requested_date"] == result["data_date"] == HOLIDAY.isoformat()
    assert result["date_origin"] == "request_parameter"
    assert result["history"][0]["trade_date"] == result["history"][0]["date"] == DAY.isoformat()
    with pytest.raises(ValueError, match="未返回请求日期"):
        await kaipanla.fetch_dataset("ext_kpl_emotion", HOLIDAY)


async def test_conflict_rejects_history_without_publishing_cache(monkeypatch):
    async def request(*args):
        return _payload([
            {"Day": DAY.isoformat(), "strong": "56"},
            {"Day": DAY.isoformat(), "strong": "99"},
        ])

    monkeypatch.setattr(kaipanla, "_request", request)
    with pytest.raises(ValueError, match="冲突记录"):
        await kaipanla.fetch_dataset("ext_kpl_emotion", DAY, include_history=True)
    assert not kaipanla._CACHE
