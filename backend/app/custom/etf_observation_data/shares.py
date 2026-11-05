"""Collect published on-exchange ETF units, retaining the original responses.

Run from backend with python -m app.custom.etf_observation_data.shares.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import logging
import os
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import mkstemp
from urllib.parse import urlencode

import httpx

from app.services.huichun_calendar import BUNDLED_CALENDAR_PATH, load_market_calendar

SSE = ("510050", "510300", "510310", "510330", "510500", "512100", "588000")
SZSE = ("159915", "159919", "159922")
QUARTER = (date(2026, 6, 29), date(2026, 6, 30), date(2026, 9, 30))
SPLIT = (date(2024, 9, 19), date(2024, 9, 20), date(2024, 9, 23))
SSE_URL = "https://query.sse.com.cn/commonQuery.do"
SZSE_URL = "https://www.szse.cn/api/report/ShowReport/data"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[4] / "data" / "etf_observation"
logger = logging.getLogger(__name__)


def shares_from_wan(value: str) -> int:
    """Both exchanges publish units in 万份 (usually to two decimal places)."""
    shares = Decimal(value.replace(",", "")) * 10_000
    if not shares.is_finite() or shares <= 0 or shares != shares.to_integral_value():
        raise ValueError(f"Invalid published units in 万份: {value!r}")
    return int(shares)


@dataclass(frozen=True)
class Query:
    url: str
    name: str
    expected: frozenset[tuple[str, str]]


def _queries(sessions: list[date]) -> list[Query]:
    requested = sorted(set(sessions) | set(QUARTER))
    result = []
    for day in sorted(set(requested) | set(SPLIT)):
        params = {
            "isPagination": "true",
            "pageHelp.pageSize": "2000",
            "pageHelp.pageNo": "1",
            "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L",
            "STAT_DATE": day.isoformat(),
        }
        symbols = SSE if day in requested else ("510310",)
        expected = frozenset((symbol, day.isoformat()) for symbol in symbols)
        result.append(Query(f"{SSE_URL}?{urlencode(params)}", f"sse_{day}", expected))

    # Query at most 20 consecutive calendar days, within the actual SZSE page size
    # of 20 records, even when the calendar includes extra historical samples.
    groups: list[list[date]] = []
    for day in requested:
        if not groups or (day - groups[-1][0]).days >= 20:
            groups.append([])
        groups[-1].append(day)
    for symbol in SZSE:
        for group in groups:
            first, last = group[0], group[-1]
            params = {
                "SHOWTYPE": "JSON",
                "CATALOGID": "scsj_fund_jjgm",
                "TABKEY": "tab1",
                "jjlb": "ETF",
                "txtDm": symbol,
                "txtStart": first.isoformat(),
                "txtEnd": last.isoformat(),
            }
            expected = frozenset((symbol, d.isoformat()) for d in group)
            name = f"szse_{symbol}_{first}_{last}"
            result.append(Query(f"{SZSE_URL}?{urlencode(params)}", name, expected))
    return result


def _rows(query: Query, body: bytes) -> dict[tuple[str, str], int]:
    payload = json.loads(body)
    if query.url.startswith(SSE_URL):
        records = payload["result"]
        if int(payload["pageHelp"]["total"]) != len(records):
            raise ValueError(f"Incomplete SSE response: {query.name}")
        keys = ("SEC_CODE", "STAT_DATE", "TOT_VOL")
    else:
        (tab,) = payload
        records = tab["data"]
        if tab.get("error") or int(tab["metadata"]["recordcount"]) != len(records):
            raise ValueError(f"Incomplete SZSE response: {query.name}")
        keys = ("fund_code", "size_date", "current_size")
    result = {}
    for row in records:
        symbol, day, value = (row[k] for k in keys)
        key = (symbol, day)
        if key in query.expected:
            if key in result:
                raise ValueError(f"Duplicate exchange observation: {key}")
            result[key] = shares_from_wan(str(value))
    return result


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = mkstemp(dir=path.parent)
    temp_path = Path(name)
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(data)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def collect_shares(
    output: Path, start: date, end: date, *, refresh: bool = False
) -> list[tuple[str, str]]:
    sessions = load_market_calendar(BUNDLED_CALENDAR_PATH, start=start, end=end)
    queries = _queries(sessions)
    raw = output / "raw" / "shares"
    with httpx.Client(timeout=25, headers={"User-Agent": "Mozilla/5.0"}) as client:

        def fetch(query: Query) -> tuple[dict[tuple[str, str], int], bytes | None]:
            path = raw / f"{query.name}.json.gz"
            if path.exists() and not refresh:
                try:
                    cached = _rows(query, gzip.decompress(path.read_bytes()))
                    if cached.keys() == query.expected:
                        return cached, None
                except (
                    gzip.BadGzipFile,
                    EOFError,
                    zlib.error,
                    ValueError,
                    KeyError,
                    TypeError,
                    ArithmeticError,
                ) as exc:
                    logger.warning("Refetching invalid exchange report %s: %s", path.name, exc)
            referer = (
                "https://www.sse.com.cn/"
                if query.url.startswith(SSE_URL)
                else "https://www.szse.cn/"
            )
            for attempt in range(3):
                try:
                    response = client.get(query.url, headers={"Referer": referer})
                    response.raise_for_status()
                    rows = _rows(query, response.content)
                    return rows, gzip.compress(response.content, mtime=0)
                except (httpx.HTTPError, json.JSONDecodeError):
                    if attempt == 2:
                        raise
                    time.sleep(2**attempt)
            raise AssertionError("unreachable")

        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(("symbol", "date", "shares", "source_url"))
        gaps = []
        observations = []
        fetched = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            for query, (rows, body) in zip(queries, pool.map(fetch, queries), strict=True):
                gaps.extend(query.expected - rows.keys())
                if body is not None:
                    fetched[raw / f"{query.name}.json.gz"] = body
                for (symbol, day), units in rows.items():
                    suffix = "SH" if symbol in SSE else "SZ"
                    observations.append((f"{symbol}.{suffix}", day, units, query.url))
        if gaps:
            return sorted(gaps)
        for path, body in fetched.items():
            _write(path, body)
        writer.writerows(sorted(observations))
        _write(output / "shares.csv", buffer.getvalue().encode("utf-8"))
    return []


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 8, 4))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 11, 4))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--refresh", action="store_true", help="Re-fetch cached exchange reports")
    args = parser.parse_args()
    missing = collect_shares(args.output_dir, args.start, args.end, refresh=args.refresh)
    if missing:
        print(f"Incomplete exchange reports; snapshot not published. Missing rows: {len(missing)}")
        for code, trading_day in missing:
            print(code, trading_day)
    else:
        print(f"Published complete exchange snapshot to {args.output_dir / 'shares.csv'}")
    raise SystemExit(int(bool(missing)))
