"""Load an independently sourced exchange calendar for historical data audits.

The file describes realized/scheduled sessions within its stated coverage, not
what every prior trading day knew about future sessions. Publication dates are
retained as evidence; callers must not use this calendar as a forecast signal.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _fields(value: object, expected: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} fields must be exactly {sorted(expected)}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} must be a non-empty string without surrounding whitespace")
    return value


def _date(value: object, label: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{label} must be YYYY-MM-DD")
    return date.fromisoformat(value)


def load_market_calendar(path: Path, *, start: date, end: date) -> list[date]:
    """Return sorted sessions, inclusive; reject incomplete or malformed evidence.

    Sources must use exchange or CNInfo HTTP(S) domains. This verifies the file
    contract, not that a linked announcement proves the supplied closure dates.
    Every row is validated even if it lies outside the requested subinterval.
    """
    if type(start) is not date or type(end) is not date or start > end:
        raise ValueError("Calendar request requires date values with start <= end")
    content = _fields(
        json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_object),
        {"version", "coverage_start", "coverage_end", "weekends_closed", "sources", "closures"},
        "Calendar",
    )
    if type(content["version"]) is not int or content["version"] != 1:
        raise ValueError("Unsupported calendar version")
    if content["weekends_closed"] is not True:
        raise ValueError("Calendar weekends_closed must be true")
    coverage_start = _date(content["coverage_start"], "coverage_start")
    coverage_end = _date(content["coverage_end"], "coverage_end")
    if coverage_start > coverage_end:
        raise ValueError("Calendar coverage_start exceeds coverage_end")
    if start < coverage_start or end > coverage_end:
        raise ValueError("Calendar coverage does not contain the requested interval")

    if not isinstance(content["sources"], list) or not content["sources"]:
        raise ValueError("Calendar sources must be a non-empty array")
    source_ids = set()
    for index, value in enumerate(content["sources"]):
        source = _fields(value, {"id", "url", "published_on", "title"}, f"Source {index}")
        source_id = _text(source["id"], "Source id")
        _text(source["title"], "Source title")
        _date(source["published_on"], "Source published_on")
        url = urlsplit(_text(source["url"], "Source url"))
        host = url.hostname or ""
        if (
            url.scheme not in {"http", "https"}
            or url.username
            or url.password
            or url.port not in {None, 80, 443}
            or not any(
                host == domain or host.endswith("." + domain)
                for domain in ("sse.com.cn", "szse.cn", "cninfo.com.cn")
            )
        ):
            raise ValueError(f"Source {index} must use an official public HTTP(S) URL")
        if source_id in source_ids:
            raise ValueError(f"Duplicate calendar source id: {source_id}")
        source_ids.add(source_id)

    if not isinstance(content["closures"], list):
        raise ValueError("Calendar closures must be an array")
    closed = set()
    for index, value in enumerate(content["closures"]):
        closure = _fields(value, {"start_date", "end_date", "source_id"}, f"Closure {index}")
        source_id = _text(closure["source_id"], "Closure source_id")
        if source_id not in source_ids:
            raise ValueError(f"Unknown calendar source id: {source_id}")
        first = _date(closure["start_date"], "Closure start_date")
        last = _date(closure["end_date"], "Closure end_date")
        if first > last or first < coverage_start or last > coverage_end:
            raise ValueError(f"Closure {index} has invalid dates or exceeds calendar coverage")
        closed.update(range(first.toordinal(), last.toordinal() + 1))

    return [
        day
        for ordinal in range(start.toordinal(), end.toordinal() + 1)
        if ordinal not in closed and (day := date.fromordinal(ordinal)).weekday() < 5
    ]
