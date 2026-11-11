"""Read a fixed ETF basket from published partitions; never query upstream on a page request."""

from __future__ import annotations

import json
import logging
import math
from collections import Counter
from datetime import date
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from typing import Literal

import polars as pl
from pydantic import BaseModel, Field, HttpUrl

from app.custom.etf_observation_data.ledger import BASKET, RESOURCES, TABLE
from app.factors.ext_factors import _timeseries_signature
from app.market_time import cn_today
from app.services.ext_data import ExtConfigStore
from app.services.huichun_calendar import BUNDLED_CALENDAR_PATH, load_market_calendar

logger = logging.getLogger(__name__)
FUND_NAMES = {
    "510300.SH": "华泰柏瑞沪深300",
    "510310.SH": "易方达沪深300",
    "510330.SH": "华夏沪深300",
    "159919.SZ": "嘉实沪深300",
    "510050.SH": "华夏上证50",
    "510500.SH": "南方中证500",
    "159922.SZ": "嘉实中证500",
    "512100.SH": "南方中证1000",
    "159915.SZ": "易方达创业板",
    "588000.SH": "华夏科创50",
}
LISTED = "listed_change_value_cny"
WHOLE = "fund_net_estimate_cny"
FIELDS = (
    "symbol",
    "date",
    "previous_date",
    "previous_listed_units",
    "listed_units",
    "unit_nav_cny",
    "listed_change_units",
    LISTED,
    "scope",
    "fund_net_units",
    WHOLE,
    "status",
    "shares_source",
    "previous_shares_source",
    "nav_source",
)


class UnknownSessionError(ValueError):
    pass


@lru_cache(maxsize=1)
def _calendar(signature: int):
    coverage = json.loads(BUNDLED_CALENDAR_PATH.read_text(encoding="utf-8-sig"))
    days = load_market_calendar(
        BUNDLED_CALENDAR_PATH,
        start=date.fromisoformat(coverage["coverage_start"]),
        end=date.fromisoformat(coverage["coverage_end"]),
    )
    return {d.isoformat(): prev.isoformat() for prev, d in pairwise(days)}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


@lru_cache(maxsize=180)
def _partition(path: Path, day: str, previous: str, signature: tuple):
    # Partitions are atomically replaced by the writer. mtime_ns and size also
    # invalidate a cached read when an existing trading day is repaired.
    try:
        records = pl.read_parquet(path, columns=FIELDS).to_dicts()
    except (OSError, pl.exceptions.PolarsError):
        logger.warning("ETF observation partition unavailable on %s", day)
        return {}
    counts = Counter(row["symbol"] for row in records)
    result = {}
    for row in records:
        symbol = row["symbol"]
        if (
            symbol not in FUND_NAMES
            or counts[symbol] != 1
            or row["date"] != day
            or row["previous_date"] != previous
        ):
            continue
        values = [
            row[k]
            for k in (
                "listed_units",
                "previous_listed_units",
                "listed_change_units",
                "unit_nav_cny",
                LISTED,
            )
        ]
        if row["status"] != "ok" or not all(_finite(v) for v in values):
            continue
        units, before, delta, nav, equivalent = values
        if (
            min(units, before, nav) <= 0
            or units - before != delta
            or not math.isclose(equivalent, delta * nav, rel_tol=1e-12, abs_tol=0.0001)
        ):
            continue
        if not all(
            isinstance(row[k], str) and row[k].startswith(("https://", "http://"))
            for k in ("shares_source", "previous_shares_source", "nav_source")
        ):
            continue
        result[symbol] = row
    return result


def _sum_complete(values, expected: int):
    values = list(values)
    if expected and len(values) == expected and all(_finite(v) for v in values):
        return math.fsum(values)
    return None


class _Citation(BaseModel):
    period_end: date
    published_on: date
    source_url: HttpUrl
    page: int = Field(strict=True, ge=1)
    report_title: str = Field(min_length=1)


class _Holder(BaseModel):
    name: str = Field(min_length=1)
    kind: Literal["direct", "named_plan"]
    units: int = Field(strict=True, ge=0)
    percent: float = Field(strict=True, ge=0, le=100)


class _DisclosureFund(_Citation):
    symbol: str
    denominator: str = Field(min_length=1)
    holders: list[_Holder]
    latest_review: _Citation
    interim_review: _Citation


class _Disclosures(BaseModel):
    reviewed_through: date
    funds: list[_DisclosureFund]


def _disclosures():
    # This independently dated research snapshot remains useful on a machine
    # with no local daily ledger. No attribution is inferred from ETF flows.
    try:
        report = _Disclosures.model_validate_json(
            (RESOURCES / "huijin_disclosures.json").read_bytes()
        )
        covered = {f.symbol for f in report.funds}
        if len(covered) != len(report.funds) or not covered.issubset(FUND_NAMES):
            raise ValueError("Invalid disclosure basket")
        for fund in report.funds:
            if any(
                not c.period_end <= c.published_on <= report.reviewed_through
                for c in (fund, fund.interim_review, fund.latest_review)
            ):
                raise ValueError("Invalid disclosure timeline")
        return report.model_dump(mode="json")
    except (OSError, ValueError):
        logger.warning("ETF holder disclosure evidence is unavailable")
        return {"reviewed_through": None, "funds": []}


class ObservationWindow:
    def __init__(self, root: Path, window: int, as_of: date | None):
        self.window = window
        scope = json.loads((RESOURCES / "scope.json").read_text(encoding="utf-8"))
        self.scope = {r["symbol"]: r for r in scope}
        if set(self.scope) != set(BASKET) or len(scope) != len(BASKET):
            raise ValueError("Incomplete scope evidence")
        for record in scope:
            if (
                record["scope"] not in {"fund_total_verified", "listed_only", "unverified"}
                or not record["reason"]
                or not record["source_url"]
                or date.fromisoformat(record["verified_from"])
                > date.fromisoformat(record["verified_through"])
            ):
                raise ValueError("Invalid scope evidence")
        self.whole = [s for s in BASKET if self.scope[s]["scope"] == "fund_total_verified"]
        self.base = root / "ext_data" / TABLE / "timeseries"
        table = ExtConfigStore(root).get(TABLE)
        self.signatures = {}
        sessions = _calendar(BUNDLED_CALENDAR_PATH.stat().st_mtime_ns)
        if table is not None:
            if table.mode != "timeseries" or table.id != TABLE:
                raise ValueError("Wrong observation table")
            signature = _timeseries_signature(self.base)
            if signature is None:
                raise OSError("Cannot read observation partitions")
            today = cn_today().isoformat()
            for folder, mtime, size in signature:
                d = folder.removeprefix("date=")
                if d in sessions and d <= today:
                    self.signatures[d] = (mtime, size)
        self.previous = {}
        if self.signatures:
            first, last = min(self.signatures), max(self.signatures)
            self.previous = {d: p for d, p in sessions.items() if first <= d <= last}
        self.dates = list(self.previous)
        self.latest_observed = max(self.signatures.keys() & self.previous.keys(), default=None)
        self.by_day = {}
        if as_of is not None and as_of.isoformat() not in self.previous:
            raise UnknownSessionError
        self.latest_complete = next((d for d in reversed(self.dates) if self._complete(d)), None)
        self.as_of = as_of.isoformat() if as_of else self.latest_complete or self.latest_observed
        self.days = [d for d in self.dates if self.as_of and d <= self.as_of][-window:]
        for day in self.days:
            self._load(day)

    def _load(self, day):
        if day in self.by_day:
            return self.by_day[day]
        data = {}
        if day in self.signatures:
            raw = _partition(
                self.base / f"date={day}" / "part.parquet",
                day,
                self.previous[day],
                self.signatures[day],
            )
            for symbol, stored in raw.items():
                record = self.scope[symbol]
                current = (
                    record["verified_from"] <= self.previous[day]
                    and day <= record["verified_through"]
                )
                scope = (
                    record["scope"]
                    if current and record["scope"] == stored["scope"]
                    else "unverified"
                )
                row = dict(stored, scope=scope)
                if (
                    scope != "fund_total_verified"
                    or row["fund_net_units"] != row["listed_change_units"]
                    or row[WHOLE] != row[LISTED]
                ):
                    row["fund_net_units"] = None
                    row[WHOLE] = None
                data[symbol] = row
        self.by_day[day] = data
        return data

    def _complete(self, day):
        rows = self._load(day)
        # Rows past their scope review are listed-only; they cannot hold a session back.
        return len(rows) == len(BASKET) and all(
            rows[s][WHOLE] is not None
            for s in self.whole
            if rows[s]["scope"] == "fund_total_verified"
        )

    def detail(self, symbol):
        rows = []
        for day in self.days:
            row = self.by_day[day].get(symbol)
            if row is None:
                row = dict.fromkeys(FIELDS)
                row.update(
                    symbol=symbol,
                    date=day,
                    previous_date=self.previous[day],
                    scope="unverified",
                    status="missing",
                )
            rows.append(row)
        return {"symbol": symbol, "as_of": self.as_of, "window": self.window, "rows": rows}

    def overview(self):
        trend = []
        for day in self.days:
            rows = self.by_day[day]
            trend.append(
                {
                    "date": day,
                    LISTED: _sum_complete((r[LISTED] for r in rows.values()), len(BASKET)),
                    WHOLE: _sum_complete(
                        (rows[s][WHOLE] for s in self.whole if s in rows), len(self.whole)
                    ),
                }
            )
        funds = []
        for symbol, name in FUND_NAMES.items():
            rows = [self.by_day[d][symbol] for d in self.days if symbol in self.by_day[d]]
            record = self.scope[symbol]
            latest = self.by_day.get(self.as_of, {}).get(symbol)
            scope = latest["scope"] if latest else "unverified"
            reason = record["reason"]
            if latest is None:
                reason = "所选交易日缺少经过核对的份额和净值。"
            elif scope != record["scope"]:
                reason = "所选交易日的全基金登记口径仍待核对。"
            funds.append(
                dict(
                    symbol=symbol,
                    name=name,
                    scope=scope,
                    reason=reason,
                    source_url=record["source_url"],
                    latest=latest,
                    days_covered=len(rows),
                    period_fund_net_estimate_cny=_sum_complete(
                        (r[WHOLE] for r in rows), self.window
                    ),
                    period_listed_change_value_cny=_sum_complete(
                        (r[LISTED] for r in rows), self.window
                    ),
                )
            )
        return dict(
            dates=self.dates,
            as_of=self.as_of,
            latest_observed_date=self.latest_observed,
            latest_complete_date=self.latest_complete,
            fund_pool_size=len(self.whole),
            listed_pool_size=len(BASKET),
            window=self.window,
            start_date=self.days[0] if self.days else None,
            session_count=len(self.days),
            totals={k: _sum_complete((r[k] for r in trend), self.window) for k in (LISTED, WHOLE)},
            trend=trend,
            funds=funds,
            disclosures=_disclosures(),
        )
