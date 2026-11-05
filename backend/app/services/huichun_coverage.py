"""Cutoff-day counts and publication checks for the Huichun daily scan."""

from collections import Counter
from datetime import date, datetime, time
from pathlib import Path

import polars as pl

from app.market_time import CN_TZ
from app.services import daily_health
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


def market_counts(raw: pl.DataFrame, day: date, previous: date | None, now: datetime) -> dict[str, int]:
    active = raw.filter(pl.col("date").is_in([previous, day]) & (pl.col("volume") > 0))
    today = active.filter(pl.col("date") == day)
    active_symbols = today["symbol"].n_unique()
    final_cutoff = datetime.combine(day, CLOSE_CUTOFF, CN_TZ)
    incomplete = 0
    if now < final_cutoff:
        incomplete = active_symbols
    elif "quote_ts" in today.columns:
        # Null/missing quote_ts marks batch history in the daily repository.
        # Close snapshots before the repository's 15:30 final cutoff remain provisional.
        end = min(now, datetime.combine(day, time.max, CN_TZ))
        stamped = today.filter(pl.col("quote_ts").is_not_null())
        incomplete = stamped.filter(
            ~pl.col("quote_ts").is_between(int(final_cutoff.timestamp() * 1000), end.timestamp() * 1000)
        )["symbol"].n_unique()
    return {
        "active_symbols": active_symbols,
        "previous_active_symbols": active.filter(pl.col("date") != day)["symbol"].n_unique(),
        "unfinalized_symbols": incomplete,
    }


def known_raw_issue(data_dir: Path, day: date) -> bool:
    try:
        issues = daily_health.snapshot(data_dir)["entries"]
    except daily_health.DailyIntegrityError:
        return True
    return any(
        row["asset_type"] == "stock" and row["day"] == str(day) and row["needs_raw"]
        for row in issues
    )


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
