"""Append newly published sessions to the ETF ledger.

Runs from the extension's scheduled job, never from a page request. Each run
tries the latest pending session first and steps back while that day's exchange
units or cross-checked NAVs are still unpublished, so the release always grows
as a gap-free prefix.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from app.custom.etf_observation_data.ledger import BASELINE, LAST, TABLE, build
from app.custom.etf_observation_data.nav import collect_nav
from app.custom.etf_observation_data.shares import collect_shares
from app.services.huichun_calendar import BUNDLED_CALENDAR_PATH, read_market_calendar

logger = logging.getLogger(__name__)
# Trying older ends only helps while the newest days are unpublished; a longer
# backlog is still covered because every build spans BASELINE..end.
MAX_ATTEMPTS = 5


def last_published(data_dir: Path) -> date | None:
    folder = data_dir / "ext_data" / TABLE / "timeseries"
    days = [p.name.removeprefix("date=") for p in folder.glob("date=*") if p.is_dir()]
    return date.fromisoformat(max(days)) if days else None


def append_new_sessions(data_dir: Path, today: date) -> list[str]:
    """Publish any newly complete sessions up to today; returns the days added."""
    last = last_published(data_dir)
    if last is None or last < LAST:
        # Bootstrapping the audited release stays a deliberate manual step.
        logger.info("ETF ledger has no audited release to extend; skipping daily append")
        return []
    calendar = read_market_calendar(BUNDLED_CALENDAR_PATH)
    if today > calendar.coverage_end:
        logger.warning("Market calendar ends %s; ETF ledger cannot append later sessions", calendar.coverage_end)
    end = min(today, calendar.coverage_end)
    if end <= last:
        return []
    pending = calendar.sessions(start=last + timedelta(days=1), end=end)
    output = data_dir / "etf_observation"
    for candidate in reversed(pending[-MAX_ATTEMPTS:]):
        missing = collect_shares(output, BASELINE, candidate)
        if missing:
            logger.info("ETF units not yet published through %s: %d rows missing", candidate, len(missing))
            continue
        missing = collect_nav(output, BASELINE, candidate)
        if missing:
            logger.info("ETF NAVs not yet confirmed through %s: %d rows missing", candidate, len(missing))
            continue
        return build(data_dir, candidate)
    return []
