"""Read-only daily coverage and Huichun A0 research audit.

The current repository lacks historical eligibility and a full market calendar.
Observed market dates provide a lower bound for gaps; shape matches remain
unverified candidates, never executable orders or validated strategy returns.
"""

# Report prose uses native Chinese punctuation.
# ruff: noqa: RUF001
from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import polars as pl

if TYPE_CHECKING:
    from app.tickflow.repository import KlineRepository

FRAME_SCHEMA = {
    "symbol": pl.String,
    "date": pl.Date,
    "close": pl.Float64,
    "segment_id": pl.Int64,
    "segment_invalidated_at": pl.Date,
    "eligible": pl.Boolean,
    "eligibility_reason": pl.String,
}
RAW_COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
FACTOR_SCHEMA = {"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64}
SUSPENSION_KNOWLEDGE_RULE = "known_at < missing_date; date-only announcements usable next day"


@dataclass(frozen=True)
class SuspensionEvidence:
    """known_at is when the complete bounded interval was publicly supported."""

    symbol: str
    start_date: date
    end_date: date
    known_at: date
    source_url: str
    title: str
    event_type: str = "full_day_suspension"


def load_suspensions(path: Path) -> tuple[SuspensionEvidence, ...]:
    """Read explicit full-day evidence; a publication date is usable from the next day.

    known_at must support the complete bounded interval, not just its start.
    A later resumption notice cannot lend its end date to an earlier notice.
    Dates carry no release time, so same-day knowledge cannot be assumed.
    Validation checks the evidence contract, not the truth of its linked source.
    """
    content = json.loads(path.read_text(encoding="utf-8-sig"))
    fields = set(SuspensionEvidence.__dataclass_fields__)
    if not isinstance(content, list):
        raise ValueError("Suspension evidence must be a JSON array")
    records = []
    for index, row in enumerate(content):
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError(
                f"Suspension evidence row {index} must contain exactly {sorted(fields)}"
            )
        if not all(isinstance(value, str) and value.strip() for value in row.values()):
            raise ValueError(f"Suspension evidence row {index} requires non-empty strings")
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", row["symbol"]):
            raise ValueError(f"Invalid suspension symbol at row {index}")
        if row["event_type"] != "full_day_suspension":
            raise ValueError(f"Unsupported suspension event_type at row {index}")
        url = urlsplit(row["source_url"])
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError(f"Suspension source_url must be a public HTTP(S) URL at row {index}")
        dates = {}
        for key in ("start_date", "end_date", "known_at"):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", row[key]):
                raise ValueError(f"Suspension {key} must be YYYY-MM-DD at row {index}")
            dates[key] = date.fromisoformat(row[key])
        if dates["start_date"] > dates["end_date"]:
            raise ValueError(f"Suspension start_date exceeds end_date at row {index}")
        records.append(SuspensionEvidence(**{**row, **dates}))
    return tuple(records)


@dataclass
class PreparedBatch:
    frame: pl.DataFrame
    coverage: pl.DataFrame


def prepare_batch(
    raw: pl.DataFrame,
    factors: pl.DataFrame,
    *,
    market_dates: list[date],
    end: date,
    listing_dates: dict[str, date],
    expected_symbols: list[str] | None = None,
    suspensions: tuple[SuspensionEvidence, ...] = (),
    calendar_verified: bool = False,
) -> PreparedBatch:
    """Validate prices, split at unknown gaps, and use a fixed causal price scale.

    ex_factor is the repository's per-event pre/post ratio (not cumulative).
    raw_close * cumprod(events effective through D) removes corporate-action
    jumps without using events after D. All A0 conditions are scale invariant.
    Absence of factor events is reported; it does not certify completeness.
    """
    calendar = sorted({d for d in market_dates if d <= end})
    if not calendar:
        raise ValueError("No observed market dates in audit range")
    day_index = {d: i for i, d in enumerate(calendar)}
    raw = raw.filter(pl.col("date") <= end).sort(["symbol", "date"])
    factors = factors.filter(pl.col("trade_date").is_null() | (pl.col("trade_date") <= end))
    symbols = sorted(set(expected_symbols or []) | set(raw["symbol"].unique().to_list()))
    frames: list[pl.DataFrame] = []
    rows: list[dict] = []
    groups = {key[0]: group for key, group in raw.partition_by("symbol", as_dict=True).items()}
    fac_groups = {
        key[0]: group for key, group in factors.partition_by("symbol", as_dict=True).items()
    }
    for symbol in symbols:
        group = groups.get(symbol, raw.head(0))
        fac = fac_groups.get(symbol, factors.head(0)).sort("trade_date")
        duplicates = group.select(pl.struct("symbol", "date").is_duplicated()).to_series()
        valid = (
            pl.all_horizontal(
                [
                    pl.col(c).is_not_null() & pl.col(c).is_finite() & (pl.col(c) > 0)
                    for c in ("open", "high", "low", "close", "volume", "amount")
                ]
            )
            & (pl.col("high") >= pl.max_horizontal("open", "close", "low"))
            & (pl.col("low") <= pl.min_horizontal("open", "close", "high"))
        )
        # quote_ts marks realtime snapshots. Null means a historical batch row.
        if "quote_ts" in group.columns:
            ts = (
                pl.from_epoch(pl.col("quote_ts"), time_unit="ms")
                .dt.replace_time_zone("UTC")
                .dt.convert_time_zone("Asia/Shanghai")
            )
            valid &= pl.col("quote_ts").is_null() | (
                (ts.dt.date() == pl.col("date")) & (ts.dt.hour() >= 15)
            )
        group = group.with_columns(
            valid.fill_null(False).alias("_valid"), duplicates.alias("_duplicate")
        )
        clean = group.filter(pl.col("_valid") & ~pl.col("_duplicate"))
        listing = listing_dates.get(symbol)
        # Known IPO dates suppress pre-listing gaps; unknown dates cannot certify coverage.
        if listing:
            clean = clean.filter(pl.col("date") >= listing)
        dates = clean["date"].to_list()
        expected = calendar[bisect_left(calendar, max(listing or calendar[0], calendar[0])) :]
        evidenced = {
            day
            for evidence in suspensions
            if evidence.symbol == symbol
            for day in expected
            if evidence.start_date <= day <= evidence.end_date
        }
        known_suspensions = {
            day
            for evidence in suspensions
            if evidence.symbol == symbol
            for day in expected
            if evidence.start_date <= day <= evidence.end_date and evidence.known_at < day
        }
        conflicts = evidenced & set(group.filter(pl.col("_valid"))["date"].to_list())
        if conflicts:
            raise ValueError(
                f"Valid daily prices conflict with full-day suspension: {symbol} {min(conflicts)}"
            )
        absent = set(expected) - set(dates)
        confirmed = sorted(absent & known_suspensions)
        # Later evidence may describe a real halt, but cannot repair past information gaps.
        timing_unverified = sorted((absent & evidenced) - known_suspensions)
        missing = sorted(absent - known_suspensions)
        fac_invalid = fac.filter(
            pl.col("trade_date").is_null()
            | pl.col("ex_factor").is_null()
            | ~pl.col("ex_factor").is_finite()
            | (pl.col("ex_factor") <= 0)
        ).height
        fac_duplicate = fac.select(pl.struct("symbol", "trade_date").is_duplicated().sum()).item()
        factor_status = (
            "invalid"
            if fac_invalid or fac_duplicate
            else ("events_present_unverified" if fac.height else "no_events_unverified")
        )
        segments: list[int] = []
        invalidated: list[date | None] = []
        segment = 0
        for i, day in enumerate(dates):
            if day not in day_index:
                raise ValueError("Raw date absent from observed calendar")
            if i:
                gap_index = bisect_right(missing, dates[i - 1])
                if gap_index < len(missing) and missing[gap_index] < day:
                    segment += 1
            segments.append(segment)
            next_day = dates[i + 1] if i + 1 < len(dates) else None
            idx = bisect_right(missing, day)
            invalidated.append(
                missing[idx]
                if idx < len(missing) and (next_day is None or missing[idx] < next_day)
                else None
            )
        lengths: dict[int, int] = {}
        for seg in segments:
            lengths[seg] = lengths.get(seg, 0) + 1
        max_bars = max(lengths.values(), default=0)
        rows.append(
            {
                "symbol": symbol,
                "raw_rows": group.height,
                "valid_rows": clean.height,
                "first_date": group["date"].min(),
                "last_date": group["date"].max(),
                "listing_date": listing,
                "listing_date_known": listing is not None,
                "invalid_rows": group.filter(~pl.col("_valid")).height,
                "duplicate_rows": int(duplicates.sum()),
                "observed_expected_days": len(expected),
                "missing_observed_days": len(missing),
                "confirmed_suspension_days": len(confirmed),
                "suspension_days_without_prior_evidence": len(timing_unverified),
                "first_missing_date": missing[0] if missing else None,
                "last_missing_date": missing[-1] if missing else None,
                "missing_prefix_days": sum(d < dates[0] for d in missing)
                if dates
                else len(missing),
                "largest_segment_bars": max_bars,
                "segment_count": len(lengths),
                "has_250_bar_segment": max_bars >= 250,
                "factor_events": fac.height,
                "factor_status": factor_status,
                "historical_eligibility": "unknown",
                "coverage_status": "price_audit_available"
                if max_bars > 250 and factor_status != "invalid"
                else "insufficient_or_invalid",
            }
        )
        if clean.is_empty() or factor_status == "invalid":
            continue
        if fac.height:
            causal = fac.with_columns(pl.col("ex_factor").cum_prod().alias("_causal_factor"))
            clean = clean.join_asof(
                causal.select("trade_date", "_causal_factor"),
                left_on="date",
                right_on="trade_date",
                strategy="backward",
            )
            clean = clean.with_columns(
                (pl.col("close") * pl.col("_causal_factor").fill_null(1)).alias("close")
            )
        if clean.filter(~pl.col("close").is_finite() | (pl.col("close") <= 0)).height:
            raise ValueError(f"Non-finite adjusted prices for {symbol}")
        frames.append(
            clean.select("symbol", "date", "close").with_columns(
                pl.Series("segment_id", segments, dtype=pl.Int64),
                pl.Series("segment_invalidated_at", invalidated, dtype=pl.Date),
                pl.lit(None, dtype=pl.Boolean).alias("eligible"),
                pl.lit(
                    "historical_status_and_factor_completeness_unverified"
                    if calendar_verified
                    else "historical_status_calendar_and_factor_completeness_unverified"
                ).alias("eligibility_reason"),
            )
        )
    return PreparedBatch(
        pl.concat(frames) if frames else pl.DataFrame(schema=FRAME_SCHEMA),
        pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame(),
    )


def _main_board(symbol: str) -> bool:
    # Same symbol families as StrategyEngine's 沪主板 / 深主板 filters.
    return (symbol.endswith(".SH") and symbol.startswith("60")) or (
        symbol.endswith(".SZ") and symbol.startswith("00")
    )


def snapshot_files(
    data_dir: Path,
    *,
    suspensions_path: Path | None = None,
    calendar_path: Path | None = None,
) -> dict:
    """Content hashes allow a run to detect concurrent changes and identify inputs."""
    paths = []
    for table in ("kline_daily", "adj_factor", "instruments"):
        for path in sorted((data_dir / table).rglob("*.parquet")):
            paths.append((path, path.relative_to(data_dir).as_posix()))
    if suspensions_path is not None:
        paths.append((suspensions_path, f"suspensions:{suspensions_path.resolve().as_posix()}"))
    if calendar_path is not None:
        paths.append((calendar_path, f"calendar:{calendar_path.resolve().as_posix()}"))
    files = []
    for path, label in paths:
        stat = path.stat()
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        files.append(
            {"path": label, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest}
        )
    encoded = json.dumps(files, sort_keys=True).encode()
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "files": files}


def _table_exists(repo: KlineRepository, name: str) -> bool:
    return bool(
        repo.execute_one(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]
        )[0]
    )


def run_audit(
    repo: KlineRepository,
    *,
    start: date,
    end: date,
    output_dir: Path,
    batch_size: int = 128,
    suspensions_path: Path | None = None,
    calendar_path: Path | None = None,
) -> dict:
    from app.strategy.huichun_a0 import CYCLE_SCHEMA, A0Params, audit_a0

    if start > end or batch_size < 1:
        raise ValueError("Invalid date range or batch size")
    if output_dir.exists():
        raise ValueError("Output directory must be new; preserve previous runs")
    snapshot_kwargs = {
        key: value
        for key, value in (("suspensions_path", suspensions_path), ("calendar_path", calendar_path))
        if value is not None
    }
    before = snapshot_files(repo.store.data_dir, **snapshot_kwargs)
    suspensions = load_suspensions(suspensions_path) if suspensions_path is not None else ()
    observed_dates = [
        r[0]
        for r in repo.execute_all(
            "SELECT DISTINCT date FROM kline_daily WHERE date <= ? ORDER BY date", [end]
        )
    ]
    if not observed_dates:
        raise ValueError("No daily observations before requested end")
    source_code_paths = [Path(__file__), Path(__file__).parents[1] / "strategy" / "huichun_a0.py"]
    calendar_source = "observed_daily_dates_lower_bound"
    dates = observed_dates
    if calendar_path is not None:
        from app.services.huichun_calendar import load_market_calendar

        dates = load_market_calendar(calendar_path, start=observed_dates[0], end=end)
        outside_calendar = sorted(set(observed_dates) - set(dates))
        if outside_calendar:
            raise ValueError(
                "Raw dates conflict with official trading calendar: "
                + ", ".join(str(day) for day in outside_calendar[:5])
            )
        calendar_source = "official_exchange_holiday_notices"
        source_code_paths.append(Path(__file__).with_name("huichun_calendar.py"))
    market_dates_without_observations = sorted(set(dates) - set(observed_dates))
    universe = repo.execute_all(
        "SELECT symbol, count(*), min(date), max(date) FROM kline_daily WHERE date <= ? GROUP BY symbol ORDER BY symbol",
        [end],
    )
    symbols = [r[0] for r in universe if _main_board(r[0])]
    inst = (
        repo.execute_all("SELECT symbol, name, listing_date FROM instruments")
        if _table_exists(repo, "instruments")
        else []
    )
    listings, names = {}, {}
    for symbol, name, listing in inst:
        names[symbol] = name
        try:
            listings[symbol] = date.fromisoformat(str(listing)[:10])
        except (ValueError, TypeError):
            continue
    symbols = sorted(set(symbols) | {s for s in listings if _main_board(s) and listings[s] <= end})
    if not symbols:
        raise ValueError("No main-board stocks available for A0 audit")
    adj_rows = (
        repo.execute_all(
            "SELECT symbol, trade_date, ex_factor FROM adj_factor WHERE trade_date <= ? OR trade_date IS NULL",
            [end],
        )
        if _table_exists(repo, "adj_factor")
        else []
    )
    factors = pl.DataFrame(adj_rows, schema=FACTOR_SCHEMA, orient="row")
    schema_names = {r[0] for r in repo.execute_all("DESCRIBE kline_daily")}
    columns = [*RAW_COLUMNS, *(["quote_ts"] if "quote_ts" in schema_names else [])]
    coverage, cycles, signals = [], [], []
    params = A0Params()
    for offset in range(0, len(symbols), batch_size):
        batch = symbols[offset : offset + batch_size]
        marks = ",".join("?" for _ in batch)
        records = repo.execute_all(
            f"SELECT {','.join(columns)} FROM kline_daily WHERE date <= ? AND symbol IN ({marks}) ORDER BY symbol,date",
            [end, *batch],
        )
        raw_schema = {
            c: pl.String if c == "symbol" else pl.Date if c == "date" else pl.Float64
            for c in columns
        }
        raw = pl.DataFrame(records, schema=raw_schema, orient="row")
        prepared = prepare_batch(
            raw,
            factors.filter(pl.col("symbol").is_in(batch)),
            market_dates=dates,
            end=end,
            listing_dates=listings,
            expected_symbols=batch,
            suspensions=suspensions,
            calendar_verified=calendar_path is not None,
        )
        coverage.append(prepared.coverage)
        if not prepared.frame.is_empty():
            audit = audit_a0(prepared.frame, start=start, end=end, params=params)
            if not audit.cycles.is_empty():
                cycles.append(audit.cycles)
            if not audit.signals.is_empty():
                signals.append(audit.signals)
        print(
            f"Audited {min(offset + batch_size, len(symbols))}/{len(symbols)} symbols", flush=True
        )
    cov = pl.concat(coverage, how="diagonal_relaxed") if coverage else pl.DataFrame()
    cy = pl.concat(cycles, how="diagonal_relaxed") if cycles else pl.DataFrame(schema=CYCLE_SCHEMA)
    sig = (
        pl.concat(signals, how="diagonal_relaxed") if signals else pl.DataFrame(schema=CYCLE_SCHEMA)
    )
    yearly = repo.execute_all(
        "SELECT year(date), count(*), count(DISTINCT symbol) FROM kline_daily WHERE date <= ? GROUP BY year(date) ORDER BY year(date)",
        [end],
    )
    after = snapshot_files(repo.store.data_dir, **snapshot_kwargs)
    if before != after:
        raise RuntimeError("Source files changed during audit; no mixed-snapshot report was saved")
    summary = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "raw_first": observed_dates[0].isoformat(),
        "raw_last": observed_dates[-1].isoformat(),
        "observed_market_days": len(observed_dates),
        "expected_market_days": len(dates),
        "market_days_without_observations": len(market_dates_without_observations),
        "market_dates_without_observations": [
            day.isoformat() for day in market_dates_without_observations
        ],
        "raw_rows": sum(r[1] for r in universe),
        "raw_symbols": len(universe),
        "main_board_symbols_audited": len(symbols),
        "symbols_with_gaps": cov.filter(pl.col("missing_observed_days") > 0).height,
        "symbols_with_prefix_gaps": cov.filter(pl.col("missing_prefix_days") > 0).height,
        "confirmed_suspension_days": cov["confirmed_suspension_days"].sum(),
        "symbols_with_confirmed_suspensions": cov.filter(
            pl.col("confirmed_suspension_days") > 0
        ).height,
        "suspension_days_without_prior_evidence": cov[
            "suspension_days_without_prior_evidence"
        ].sum(),
        "suspension_evidence_records": len(suspensions),
        "symbols_with_250_bar_segment": cov.filter(pl.col("has_250_bar_segment")).height,
        "invalid_rows": cov["invalid_rows"].sum(),
        "duplicate_rows": cov["duplicate_rows"].sum(),
        "cycles": cy.height,
        "shape_candidates": sig.height,
        "verified_signals": 0,
        "eligibility_status": "unverified",
        "calendar_source": calendar_source,
    }
    output_dir.mkdir(parents=True)
    for name, frame in (("coverage_by_symbol", cov), ("a0_cycles", cy), ("a0_signals", sig)):
        # CSV is flattened for inspection, parquet retains native date and boolean types.
        frame.write_parquet(output_dir / f"{name}.parquet")
        export = frame
        for column, dtype in frame.schema.items():
            if isinstance(dtype, pl.List):
                export = export.with_columns(pl.col(column).list.join(";").alias(column))
        export.write_csv(output_dir / f"{name}.csv", include_bom=True)
    manifest = {
        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "parameters": asdict(params),
        "summary": summary,
        "inputs": before,
        "suspension_evidence": {
            "knowledge_rule": SUSPENSION_KNOWLEDGE_RULE,
            "records": [asdict(evidence) for evidence in suspensions],
        },
        "price_basis": "raw_close_times_cumulative_effective_event_factor",
        "scope": "A0 daily shape audit; no execution or return calculation",
        "source_code_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_code_paths
        },
    }
    (output_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (output_dir / "coverage_report.md").write_text(
        _report(summary, cov, cy, sig, yearly, names), encoding="utf-8"
    )
    return summary


def _report(summary, cov, cycles, signals, yearly, names) -> str:
    independent_calendar = summary["calendar_source"] == "official_exchange_holiday_notices"
    limits = (
        "独立交易日历已依据交易所休市公告核验，股票历史资格和复权事件完整性尚未核验"
        if independent_calendar
        else "股票历史资格、独立交易日历和复权事件完整性尚未核验"
    )
    calendar_note = (
        "1. 交易日集合来自独立的交易所休市公告日历，可检查全市场整日缺失；行情落在非交易日会中止审计。"
        if independent_calendar
        else "1. 交易日集合来自全市场本地日线实际出现日期，只能发现部分缺口，无法发现全市场整日缺失。未把周一至周五当作交易日。"
    )
    lines = [
        "# 回春 A0 数据覆盖与信号审计",
        "",
        f"本报告检查历史数据及日线形态。{limits}，因此形态候选不能视为已验证交易信号；本次未计算策略收益。",
        "",
        f"信号审计区间：{summary['start']} 至 {summary['end']}。指标从每段最早可用历史初始化。",
        "",
        "## 实际数据覆盖",
        "",
        f"- 日线：{summary['raw_first']} 至 {summary['raw_last']}，{summary['observed_market_days']} 个已观测日期，{summary['raw_rows']:,} 行，{summary['raw_symbols']} 只股票。",
        f"- 审计日历包含 {summary['expected_market_days']} 个交易日；其中全市场无行情的交易日 {summary['market_days_without_observations']} 个。"
        if independent_calendar
        else "- 本次仍用已观测日期作为日历下界，无法确认全市场整日缺口。",
        f"- 主板审计：{summary['main_board_symbols_audited']} 只（含维表中有上市日期但本地无日线者）。",
        f"- 有未知缺口：{summary['symbols_with_gaps']} 只；其中上市后前缀缺失：{summary['symbols_with_prefix_gaps']} 只。",
        f"- 有事前公告证据的全日停牌：{summary['confirmed_suspension_days']} 个股日，涉及 {summary['symbols_with_confirmed_suspensions']} 只；未生成日线，指标仅沿有效行情继续。",
        f"- 有停牌公告但公开时点不足：{summary['suspension_days_without_prior_evidence']} 个股日，继续记为未知缺口。",
        f"- 至少一段达到 250 根有效日线：{summary['symbols_with_250_bar_segment']} 只；这不保证存在完整的上涨与回春周期。",
        f"- 主板无效行情行：{summary['invalid_rows']}；重复键涉及行：{summary['duplicate_rows']}。",
        "",
        "| 年份 | 原始日线行数 | 当年出现股票数（全部板块） |",
        "| --- | ---: | ---: |",
    ]
    lines.extend(f"| {year} | {count:,} | {n} |" for year, count, n in yearly)
    lines += [
        "",
        "## 缺口示例",
        "",
        "以下按未知缺失日期数降序展示。仅扣除有事前公告证据的全日停牌；其余停牌、退市和供应商漏数仍缺乏完整历史状态，不能自行补零或跳过。",
        "",
        "| 股票 | 当前名称 | 维表上市日期 | 本地首条 | 未知缺失交易日 |",
        "| --- | --- | --- | --- | ---: |",
    ]
    for row in cov.sort("missing_observed_days", descending=True).head(15).iter_rows(named=True):
        lines.append(
            f"| {row['symbol']} | {names.get(row['symbol'], '')} | {row['listing_date']} | {row['first_date']} | {row['missing_observed_days']} |"
        )
    lines += [
        "",
        "## A0 审计结果",
        "",
        f"- 周期记录：{summary['cycles']} 条；价格形态候选：{summary['shape_candidates']} 条。",
        "- 正式信号资格全部未核验，不能把 0 条已验证信号解释成策略没有机会。",
        "- 全部周期见 a0_cycles.csv；全部形态候选见 a0_signals.csv；逐股覆盖见 coverage_by_symbol.csv。",
        "",
    ]
    if not cycles.is_empty():
        lines += ["| 周期状态 | 数量 |", "| --- | ---: |"]
        lines.extend(
            f"| {s} | {n} |" for s, n in cycles.group_by("status").len().sort("status").iter_rows()
        )
    lines += [
        "",
        "## 数据口径和限制",
        "",
        calendar_note,
        "2. 历史 ST、退市整理和停牌状态尚未完整核验；当前股票名称只用于展示，不用于回筛历史。全市场退市股票覆盖未获证明。",
        "3. 因子按生效日累积，使用固定基准的因果复权收盘价。无因子事件记录不等于已证明无除权事件，事件全集仍未核验。",
        "4. 原始价无效、重复键、无成交或盘中快照作为未知缺口；缺口中断周期，下一有效段重新预热。",
        "5. MACD 参数 10/20/9，前轮收盘涨幅 40%，双线零轴距离 2%，价格高于 MA60 且 MA60 高于五根有效日线之前。每轮只检查首次后续金叉。",
        "6. 所有输入文件记录内容哈希，并在审计结束时再次校验；源文件变化会中止输出。报告和程序不修改行情文件。",
        "7. 分钟信号、费用、成交、持仓和原表其余验收属于后续阶段。本报告不代表完整回测通过。",
        "8. missing_observed_days 继续表示所选日历下的未知缺口，已扣除 confirmed_suspension_days。停牌证据的 known_at 必须是完整有界停牌区间已获公开证据支持的日期，不能拼接早期停牌公告日与事后才确定的终点。日期级公告按当日收盘后才确定，只有 known_at 严格早于缺行日期才豁免；事后确诊停牌不等于当时已知。证据和日历文件随行情输入一起做运行前后哈希校验，证据及来源见 run_manifest.json。",
        "",
    ]
    if summary["market_dates_without_observations"]:
        lines += [
            "## 全市场无行情的交易日",
            "",
            ", ".join(summary["market_dates_without_observations"]),
            "",
        ]
    return "\n".join(lines)
