"""Shared read-only raw history, calendar and quarantine inputs for Huichun A0.

Constructing a reader never initializes the mode's saved state or starts a job.
All callers use the same historical input preparation before calculating A0.
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import polars as pl

from app.price_limits import GEM_REGISTRATION_DATE, MAIN_BOARD_LIMIT, price_limit_pct
from app.services.huichun_audit import (
    FACTOR_SCHEMA,
    RAW_COLUMNS,
    PreparedBatch,
    _main_board,
    _table_exists,
    prepare_batch,
)
from app.services.huichun_calendar import BUNDLED_CALENDAR_PATH, read_market_calendar

_DEFAULT_CALENDAR = object()


def _within_daily_cutoff(relative_path: str, cutoff: date) -> bool:
    # Only the repository's canonical dated layout proves an entire file is
    # outside a query. Legacy/mixed daily files and all factor tables stay guarded.
    parts = relative_path.split("/")
    if len(parts) != 3 or parts[0] != "kline_daily" or not parts[1].startswith("date="):
        return True
    label = parts[1][5:]
    try:
        day = date.fromisoformat(label)
    except ValueError:
        return True
    return day.isoformat() != label or day <= cutoff


def screening_stock(symbol: str) -> bool:
    # Match the existing stock board families, including STAR depositary receipts.
    # The daily stock table is separate from ETF/index tables; suffix checks also
    # prevent malformed or misplaced rows from entering the candidate universe.
    return _main_board(symbol) or (
        symbol.endswith(".SZ") and symbol.startswith(("300", "301"))
    ) or (
        symbol.endswith(".SH") and symbol.startswith(("688", "689"))
    )


class HuichunDataReader:
    def __init__(self, repo, *, calendar_path=_DEFAULT_CALENDAR):
        self.repo = repo
        # Explicit None disables the calendar; omission resolves the same override
        # as the mode. This also preserves callers with a deliberately absent calendar.
        if calendar_path is _DEFAULT_CALENDAR:
            root = repo.store.data_dir / "user_data" / "huichun"
            calendar_path = next((path for path in (
                root / "market_calendar.json", BUNDLED_CALENDAR_PATH,
            ) if path.exists()), None)
        self.calendar_path: Path | None = calendar_path

    def input_signature(self, cutoff: date | None = None) -> list:
        root = self.repo.store.data_dir
        signature = []
        for table in ("kline_daily", "adj_factor", "instruments"):
            for path in sorted((root / table).rglob("*.parquet")):
                key = path.relative_to(root).as_posix()
                if cutoff is not None and not _within_daily_cutoff(key, cutoff):
                    continue
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue  # A concurrent removal is observed as absence.
                signature.append((key, stat.st_size, stat.st_mtime_ns, stat.st_ino))
        if self.calendar_path and self.calendar_path.exists():
            stat = self.calendar_path.stat()
            signature.append(("calendar", stat.st_size, stat.st_mtime_ns))
        return signature

    def factors(self, symbols, end) -> pl.DataFrame:
        if not symbols or not _table_exists(self.repo, "adj_factor"):
            return pl.DataFrame(schema=FACTOR_SCHEMA)
        marks = ",".join("?" for _ in symbols)
        rows = self.repo.execute_all(
            "SELECT symbol,trade_date,ex_factor FROM adj_factor "
            f"WHERE (trade_date <= ? OR trade_date IS NULL) AND symbol IN ({marks}) "
            "ORDER BY symbol,trade_date",
            [end, *symbols],
        )
        return pl.DataFrame(rows, schema=FACTOR_SCHEMA, orient="row")

    def raw(self, symbols, end, *, start=None) -> pl.DataFrame:
        columns = [*RAW_COLUMNS]
        available = {r[0]: r[1] for r in self.repo.execute_all("DESCRIBE kline_daily")}
        if "quote_ts" in available:
            columns.append("quote_ts")
        if available.get("daily_batch_finalized") == "BOOLEAN":
            columns.append("daily_batch_finalized")
        clauses, args = ["date <= ?"], [end]
        if start is not None:
            clauses.append("date >= ?")
            args.append(start)
        if symbols is not None:
            marks = ",".join("?" for _ in symbols)
            clauses.append(f"symbol IN ({marks})")
            args.extend(symbols)
        frame = self.repo.execute_frame(
            f"SELECT {','.join(columns)} FROM kline_daily WHERE {' AND '.join(clauses)} ORDER BY symbol,date",
            args,
        )
        schema = {
            c: pl.String if c == "symbol" else pl.Date if c == "date" else pl.Float64
            for c in columns
        }
        if "daily_batch_finalized" in schema:
            schema["daily_batch_finalized"] = pl.Boolean
        return frame.cast(schema)

    @staticmethod
    def factor_failure_dates(raw, factors, calendar) -> dict[str, date | None]:
        """Reject implausible adjusted jumps; this is a data-quality heuristic.

        Board limits plus a two-point/tick margin retain the existing main-board
        gate. These do not establish historical trading eligibility or model IPO
        sessions without price limits.
        """
        bad: dict[str, date | None] = {}

        def mark(symbol, day):
            if symbol not in bad or day is None or (bad[symbol] is not None and day < bad[symbol]):
                bad[symbol] = day

        malformed = factors.filter(
            pl.col("trade_date").is_null()
            | pl.col("ex_factor").is_null()
            | ~pl.col("ex_factor").is_finite()
            | (pl.col("ex_factor") <= 0)
            | pl.struct("symbol", "trade_date").is_duplicated()
        )
        for symbol, day in malformed.select("symbol", "trade_date").iter_rows():
            mark(symbol, day)
        if factors.is_empty() or raw.is_empty():
            return bad
        previous = {calendar[i]: calendar[i - 1] for i in range(1, len(calendar))}
        prices = {(s, d): c for s, d, c in raw.select("symbol", "date", "close").iter_rows()}
        first_dates = {
            s: group["date"].min()
            for (s,), group in raw.partition_by("symbol", as_dict=True).items()
        }
        for symbol, day, factor in factors.iter_rows():
            if day is None or (symbol in bad and (bad[symbol] is None or day >= bad[symbol])):
                continue
            if factor == 1:
                continue
            old, current = prices.get((symbol, previous.get(day))), prices.get((symbol, day))
            if day <= first_dates.get(symbol, day):
                continue  # Events before the available price history only change its common scale.
            if not old or not current or not math.isfinite(old) or not math.isfinite(current):
                mark(symbol, day)
                continue
            limit = price_limit_pct(symbol, day)
            # The shared helper supplies current board limits. GEM's historical
            # 10% regime also matters when auditing old adjustment events.
            if symbol.startswith(("300", "301")) and day < GEM_REGISTRATION_DATE:
                limit = MAIN_BOARD_LIMIT
            if abs(current * factor / old - 1) > limit + 0.02 + 0.01 / old:
                mark(symbol, day)
        return bad

    @staticmethod
    def prepare_batch(
        raw, factors, *, calendar, end, listings, expected_symbols,
        calendar_verified: bool,
    ) -> tuple[PreparedBatch, dict[str, date | None]]:
        """Quarantine from the first dated factor failure, preserving prior signals.

        An undated event cannot be assigned to a safe prefix. Dated failures
        remain quarantined until the source is corrected; restarting indicators
        after a bad corporate action would silently invent a valid price basis.
        """
        # Uncovered history cannot seed indicators in an independently verified window.
        raw = raw.filter(pl.col("date").is_between(calendar[0], end))
        factors = factors.filter(pl.col("trade_date").is_null() | (pl.col("trade_date") <= end))
        issues = HuichunDataReader.factor_failure_dates(raw, factors, calendar)
        undated = [symbol for symbol, day in issues.items() if day is None]
        raw = raw.filter(~pl.col("symbol").is_in(undated))
        factors = factors.filter(~pl.col("symbol").is_in(undated))
        dated = [(symbol, day) for symbol, day in issues.items() if day is not None]
        if dated:
            cutoffs = pl.DataFrame(
                dated, schema={"symbol": pl.String, "_invalid_from": pl.Date}, orient="row",
            )
            raw = raw.join(cutoffs, on="symbol", how="left").filter(
                pl.col("_invalid_from").is_null() | (pl.col("date") < pl.col("_invalid_from"))
            ).drop("_invalid_from")
            factors = factors.join(cutoffs, on="symbol", how="left").filter(
                pl.col("_invalid_from").is_null()
                | (pl.col("trade_date") < pl.col("_invalid_from"))
            ).drop("_invalid_from")
        prepared = prepare_batch(
            raw, factors, market_dates=calendar, end=end, listing_dates=listings,
            expected_symbols=[s for s in expected_symbols if s not in undated],
            calendar_verified=calendar_verified,
        )
        return prepared, issues

    def calendar(self, as_of: date) -> tuple[list[date], str]:
        """Resolve sessions through a fixed cutoff without changing any mode state."""
        source = "observed_market_daily_dates"
        if not _table_exists(self.repo, "kline_daily"):
            return [], source
        observed = [row[0] for row in self.repo.execute_all(
            "SELECT DISTINCT date FROM kline_daily WHERE date <= ? ORDER BY date", [as_of],
        )]
        if not observed or self.calendar_path is None:
            return observed, source
        try:
            evidence = read_market_calendar(self.calendar_path)
            first = max(observed[0], evidence.coverage_start)
            if as_of < first:
                return [], source
            calendar = evidence.sessions(start=first, end=as_of)
        except (OSError, ValueError):
            return observed, source
        if {d for d in observed if d >= first} - set(calendar):
            raise ValueError("日线日期与独立交易日历冲突，请检查数据")
        return calendar, "official_exchange_calendar"
