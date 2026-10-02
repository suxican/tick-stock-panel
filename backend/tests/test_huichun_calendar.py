import json
from datetime import date, datetime
from pathlib import Path

import pytest

from app.services.huichun_calendar import load_market_calendar


@pytest.fixture
def calendar_data():
    return {
        "version": 1,
        "coverage_start": "2025-01-01",
        "coverage_end": "2025-01-12",
        "weekends_closed": True,
        "sources": [
            {
                "id": "sse-2025",
                "url": "https://www.sse.com.cn/notice.shtml",
                "published_on": "2024-12-23",
                "title": "Exchange holiday notice",
            }
        ],
        "closures": [
            {"start_date": "2025-01-01", "end_date": "2025-01-01", "source_id": "sse-2025"}
        ],
    }


def load(tmp_path, data, *, start=date(2025, 1, 1), end=date(2025, 1, 12)):
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return load_market_calendar(path, start=start, end=end)


def test_calendar_excludes_closures_and_weekends_and_clips_request(tmp_path, calendar_data):
    assert load(tmp_path, calendar_data, end=date(2025, 1, 6)) == [
        date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)
    ]
    assert load(tmp_path, calendar_data, start=date(2025, 1, 4), end=date(2025, 1, 5)) == []


@pytest.mark.parametrize("value", [0, 2, True, 1.0, "1", None])
def test_calendar_rejects_unsupported_version(tmp_path, calendar_data, value):
    calendar_data["version"] = value
    with pytest.raises(ValueError, match="version"):
        load(tmp_path, calendar_data)


@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_calendar_requires_explicit_weekend_closure(tmp_path, calendar_data, value):
    calendar_data["weekends_closed"] = value
    with pytest.raises(ValueError, match="weekends_closed"):
        load(tmp_path, calendar_data)


@pytest.mark.parametrize("data", [[], None, {}, {"version": 1}])
def test_calendar_rejects_invalid_document_shape(tmp_path, data):
    with pytest.raises(ValueError, match="fields"):
        load(tmp_path, data)


@pytest.mark.parametrize("field", ["coverage_start", "coverage_end"])
@pytest.mark.parametrize("value", ["20250101", "2025-02-30", "2025-1-1", None, 20250101])
def test_calendar_rejects_invalid_coverage_date(tmp_path, calendar_data, field, value):
    calendar_data[field] = value
    with pytest.raises(ValueError):
        load(tmp_path, calendar_data)


@pytest.mark.parametrize(
    "start,end",
    [(date(2024, 12, 31), date(2025, 1, 3)),
     (date(2025, 1, 3), date(2025, 1, 13)),
     (date(2025, 1, 3), date(2025, 1, 2)),
     (datetime(2025, 1, 1), date(2025, 1, 3))],
)
def test_calendar_rejects_uncovered_or_invalid_request(tmp_path, calendar_data, start, end):
    with pytest.raises(ValueError):
        load(tmp_path, calendar_data, start=start, end=end)


@pytest.mark.parametrize(
    "field,value",
    [("id", ""), ("title", " "), ("published_on", "2024-02-30"),
     ("url", "file:///calendar.json"), ("url", "https://localhost/notice"),
     ("url", "https://www.sse.com.cn.evil.example/notice"),
     ("url", "https://user:secret@www.sse.com.cn/notice"),
     ("url", "https://www.sse.com.cn:bad/notice")],
)
def test_calendar_rejects_invalid_source(tmp_path, calendar_data, field, value):
    calendar_data["sources"][0][field] = value
    with pytest.raises(ValueError):
        load(tmp_path, calendar_data)


def test_calendar_rejects_duplicate_source_id(tmp_path, calendar_data):
    calendar_data["sources"].append(dict(calendar_data["sources"][0]))
    with pytest.raises(ValueError, match="Duplicate"):
        load(tmp_path, calendar_data)


@pytest.mark.parametrize(
    "field,value",
    [("source_id", "missing"), ("start_date", "2025-01-02"),
     ("start_date", "2024-12-31"), ("end_date", "2025-01-13"),
     ("end_date", "2025-02-30"), ("source_id", None)],
)
def test_calendar_rejects_invalid_closure(tmp_path, calendar_data, field, value):
    calendar_data["closures"][0][field] = value
    with pytest.raises(ValueError):
        load(tmp_path, calendar_data)


@pytest.mark.parametrize("field,value", [("sources", []), ("sources", {}), ("closures", {})])
def test_calendar_rejects_invalid_collections(tmp_path, calendar_data, field, value):
    calendar_data[field] = value
    with pytest.raises(ValueError):
        load(tmp_path, calendar_data)


def test_calendar_rejects_unknown_fields_and_reversed_coverage(tmp_path, calendar_data):
    calendar_data["closures"][0]["unexpected"] = True
    with pytest.raises(ValueError, match="fields"):
        load(tmp_path, calendar_data)
    calendar_data["closures"][0].pop("unexpected")
    calendar_data["coverage_start"] = "2025-01-13"
    with pytest.raises(ValueError, match="coverage"):
        load(tmp_path, calendar_data)


def test_calendar_rejects_duplicate_json_keys(tmp_path):
    path = tmp_path / "calendar.json"
    path.write_text('{"version": 1, "version": 2}', encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate JSON"):
        load_market_calendar(path, start=date(2025, 1, 1), end=date(2025, 1, 2))


def test_calendar_unions_overlapping_closures_without_duplicates(tmp_path, calendar_data):
    calendar_data["closures"].append(
        {"start_date": "2025-01-01", "end_date": "2025-01-03", "source_id": "sse-2025"}
    )
    assert load(tmp_path, calendar_data, end=date(2025, 1, 6)) == [date(2025, 1, 6)]


def test_official_calendar_includes_pandemic_extension_and_ignores_makeup_weekends():
    evidence = (
        Path(__file__).resolve().parents[2]
        / "docs/策略/回春策略/evidence/exchange_calendar_2019_2026.json"
    )
    days = load_market_calendar(evidence, start=date(2019, 9, 12), end=date(2026, 12, 31))
    assert date(2020, 1, 31) not in days
    assert date(2020, 2, 3) in days
    assert date(2024, 2, 9) not in days
    assert date(2024, 2, 19) in days
    assert date(2025, 2, 8) not in days
    assert date(2026, 2, 23) not in days
    assert date(2026, 2, 24) in days
    assert date(2026, 9, 25) not in days
    assert date(2026, 10, 8) in days
    assert days[0] == date(2019, 9, 12)
    assert days[-1] == date(2026, 12, 31)
    assert len(days) == len(set(days))
    assert all(day.weekday() < 5 for day in days)
