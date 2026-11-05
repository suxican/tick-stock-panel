"""Cutoff-day counts and publication checks for the Huichun daily scan."""

from collections import Counter
from datetime import date, datetime
from pathlib import Path

import polars as pl

from app.market_time import CN_TZ
from app.services import daily_health
from app.services.daily_confirmation import FINALIZED, PENDING_REFRESH, valid_official_bar
from app.services.data_integrity import CLOSE_CUTOFF
from app.strategy.huichun_a0 import A0AuditResult

# A sharp fall from yesterday's traded universe can indicate a partly synced batch.
# This publication gate does not assert historical eligibility or suspension status.
MIN_DAILY_COVERAGE_RATIO = 0.95
REJECTION_REASONS = (
    "zero_distance_exceeded", "close_not_above_ma", "ma_not_rising", "indicator_history_insufficient",
)


def stage_counts(audit: A0AuditResult, day: date, warmup: int) -> tuple[dict[str, int], Counter[str]]:
    terminal = audit.indicators.filter(pl.col("date") == day)
    qualified = audit.cycles.filter(pl.col("rally_qualified"))
    crosses = qualified.filter(pl.col("r_date") == day)
    rejected = crosses.filter(pl.col("status") == "first_cross_rejected")
    reasons = Counter(reason for text in rejected["reasons"] for reason in text.split(";"))
    return {
        "valid_close": terminal.height,
        "warmup_ready": terminal.filter(pl.col("prior_bars") >= warmup).height,
        "qualified_waiting_first_cross": qualified.filter(
            (pl.col("status") == "waiting_first_cross") & (pl.col("last_observation_date") == day)
        ).height,
        "first_cross_today": crosses.height,
        "first_cross_rejected_today": rejected.height,
        "confirmed_today": crosses.filter(pl.col("shape_match")).height,
    }, reasons


def daily_check(
    raw: pl.DataFrame, day: date, previous: date | None, now: datetime,
) -> tuple[dict[str, int], list[str]]:
    if raw.is_empty():
        return {"active_symbols": 0, "previous_active_symbols": 0, "finalized_symbols": 0,
                "unfinalized_symbols": 0}, []
    # Positive turnover in either field establishes activity; a malformed other field
    # must not hide a traded stock from the denominator.
    traded = (pl.col("volume") > 0) | (pl.col("amount") > 0)
    today = raw.filter(pl.col("date") == day)
    active = set(today.filter(traded)["symbol"])
    old = set(raw.filter((pl.col("date") == pl.lit(previous, dtype=pl.Date)) & traded)["symbol"])
    confirmed = set()
    if now >= datetime.combine(day, CLOSE_CUTOFF, CN_TZ) and today.schema.get(FINALIZED) == pl.Boolean:
        confirmed = set(today.filter(
            pl.col(FINALIZED) & ~pl.col("symbol").is_duplicated() & valid_official_bar(today)
        )["symbol"])
    pending = active - confirmed
    needs = pending | (old - active) if len(active) < MIN_DAILY_COVERAGE_RATIO * len(old) else pending
    return {
        "active_symbols": len(active), "previous_active_symbols": len(old),
        "finalized_symbols": len(confirmed), "unfinalized_symbols": len(pending),
    }, sorted(needs)


def market_counts(raw: pl.DataFrame, day: date, previous: date | None, now: datetime) -> dict[str, int]:
    return daily_check(raw, day, previous, now)[0]


def known_raw_issues(data_dir: Path, day: date) -> tuple[bool, list[str]]:
    try:
        issues = daily_health.snapshot(data_dir)["entries"]
    except daily_health.DailyIntegrityError:
        return True, []
    pending = [r for r in issues if r["asset_type"] == "stock" and r["day"] == str(day)
               and (r["needs_raw"] or r["reason"] == PENDING_REFRESH)]
    return bool(pending), sorted({r["symbol"] for r in pending if r["symbol"]})


def known_raw_issue(data_dir: Path, day: date) -> bool:
    return known_raw_issues(data_dir, day)[0]


def publication_status(
    day: date, expected: date | None, observed: date | None, counts: Counter[str], raw_issue: bool,
) -> str:
    if expected is None:
        return "calendar_unverified"
    if day != observed:
        return "historical"
    if day != expected:
        return "stale"
    previous = counts["previous_active_symbols"]
    if (raw_issue or counts["unfinalized_symbols"] or previous == 0
            or counts["active_symbols"] < MIN_DAILY_COVERAGE_RATIO * previous):
        return "incomplete"
    return "ready"
