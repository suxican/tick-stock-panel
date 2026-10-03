"""Read existing data and publish explicit available-data Huichun experiments."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from app.backtest.huichun_daily_proxy import simulate_daily_proxy
from app.backtest.huichun_events import event_study, summarize_events
from app.services.huichun_audit import (
    FACTOR_SCHEMA,
    RAW_COLUMNS,
    load_suspensions,
    prepare_batch,
    snapshot_files,
)
from app.services.huichun_calendar import load_market_calendar
from app.strategy.huichun_a0 import A0Params, audit_a0


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _research_snapshot(repo, paths: list[Path]) -> dict:
    market = snapshot_files(repo.store.data_dir)
    evidence = [{"path": str(path.resolve()), "sha256": _hash(path)} for path in paths]
    minute = [
        {"path": str(path.relative_to(repo.store.data_dir)), "sha256": _hash(path)}
        for path in sorted((repo.store.data_dir / "kline_minute").rglob("*.parquet"))
    ]
    raw_basis = repo.store.data_dir / "kline_minute/.raw_basis"
    return {
        "market": market,
        "evidence": evidence,
        "minute": minute,
        "minute_raw_basis": _hash(raw_basis) if raw_basis.exists() else None,
    }


def load_research_daily(repo, signals, market_dates, end, suspensions, params, factors_path=None):
    """Use the same validated daily history, factors and A0 indicators as the audit."""
    symbols = sorted(signals["symbol"].unique().to_list())
    if not symbols:
        raise ValueError("No A0 candidates in source audit")
    marks = ",".join("?" for _ in symbols)
    columns = list(RAW_COLUMNS)
    names = {r[0] for r in repo.execute_all("DESCRIBE kline_daily")}
    if "quote_ts" in names:
        columns.append("quote_ts")
    schema = {
        c: pl.String if c == "symbol" else pl.Date if c == "date" else pl.Float64 for c in columns
    }
    raw = pl.DataFrame(
        repo.execute_all(
            f"SELECT {','.join(columns)} FROM kline_daily WHERE date<=? AND symbol IN ({marks}) ORDER BY symbol,date",
            [end, *symbols],
        ),
        schema=schema,
        orient="row",
    )
    factors = pl.DataFrame(
        repo.execute_all(
            f"SELECT symbol,trade_date,ex_factor FROM adj_factor WHERE (trade_date<=? OR trade_date IS NULL) AND symbol IN ({marks}) ORDER BY symbol,trade_date",
            [end, *symbols],
        ),
        schema=FACTOR_SCHEMA,
        orient="row",
    )
    if factors_path is not None:
        factors = (
            pl.read_parquet(factors_path)
            .select(*FACTOR_SCHEMA)
            .cast(FACTOR_SCHEMA)
            .filter(
                (pl.col("trade_date").is_null() | (pl.col("trade_date") <= end))
                & pl.col("symbol").is_in(symbols)
            )
        )
    if (
        factors.filter(
            pl.col("trade_date").is_null()
            | pl.col("ex_factor").is_null()
            | ~pl.col("ex_factor").is_finite()
            | (pl.col("ex_factor") <= 0)
        ).height
        or factors.select(pl.struct("symbol", "trade_date").is_duplicated().any()).item()
    ):
        raise ValueError("Signal candidates have unknown or invalid factors; rerun the audit")
    listings = {}
    for symbol, listing in repo.execute_all("SELECT symbol,listing_date FROM instruments"):
        try:
            listings[symbol] = date.fromisoformat(str(listing)[:10])
        except (TypeError, ValueError):
            continue
    prepared = prepare_batch(
        raw,
        factors,
        market_dates=market_dates,
        end=end,
        listing_dates=listings,
        expected_symbols=symbols,
        suspensions=suspensions,
        calendar_verified=True,
    )
    indicators = audit_a0(prepared.frame, start=market_dates[0], end=end, params=params).indicators
    daily = raw.join(
        indicators.select("symbol", "date", "ma60", pl.col("close").alias("adj_close")),
        on=["symbol", "date"],
        how="inner",
    )
    daily = (
        daily.sort(["symbol", "date"])
        .with_columns(
            (pl.col("adj_close") / pl.col("close")).alias("scale"),
            pl.col("amount").rolling_mean(20, min_samples=20).over("symbol").alias("rank_amount"),
            (pl.col("volume").rolling_mean(20, min_samples=20).shift(1).over("symbol") * 100).alias(
                "prior_volume_shares"
            ),
            pl.col("date").shift(1).over("symbol").alias("previous_observed_date"),
        )
        .with_columns(
            (pl.col("high") * pl.col("scale")).alias("adj_high"),
            (pl.col("low") * pl.col("scale")).alias("adj_low"),
            (
                pl.col("close").shift(1).over("symbol")
                * pl.col("scale").shift(1).over("symbol")
                / pl.col("scale")
            ).alias("reference_close"),
        )
    )
    calendar_frame = pl.DataFrame(
        {"date": market_dates, "previous_market_date": [None, *market_dates[:-1]]}
    )
    daily = daily.join(calendar_frame, on="date").with_columns(
        pl.when(pl.col("previous_market_date") == pl.col("previous_observed_date"))
        .then(pl.col("reference_close"))
        .otherwise(None)
        .alias("reference_close")
    )
    return daily, factors


def _write_rows(path: Path, rows: list[dict], empty_columns: tuple[str, ...]):
    frame = (
        pl.DataFrame(rows, infer_schema_length=None)
        if rows
        else pl.DataFrame(schema=dict.fromkeys(empty_columns, pl.String))
    )
    frame.write_csv(path, include_bom=True)


def validate_factor_prices(daily: pl.DataFrame, factors: pl.DataFrame) -> None:
    """Reject incompatible event factors under the ordinary-main-board proxy assumption.

    Only adjacent observed market days have reference_close. This is a quality
    gate, not proof that the event list is complete or a substitute price feed.
    """
    affected = daily.join(
        factors.filter(pl.col("ex_factor") != 1).select(
            "symbol", pl.col("trade_date").alias("date")
        ),
        on=["symbol", "date"],
        how="semi",
    )
    bad = affected.filter(
        pl.col("reference_close").is_not_null()
        & (
            (pl.col("close") / pl.col("reference_close") - 1).abs()
            > 0.12 + 0.01 / pl.col("reference_close")
        )
    )
    if not bad.is_empty():
        examples = bad.select("symbol", "date").head(3).rows()
        raise ValueError(
            f"Event factors conflict with ordinary-main-board price continuity: {examples}; "
            "rebuild factors from source events and rerun the signal audit"
        )


def run_research(
    repo,
    *,
    audit_dir: Path,
    plan_path: Path,
    calendar_path: Path,
    suspensions_path: Path,
    output_dir: Path,
    factors_path: Path | None = None,
) -> dict:
    if output_dir.exists():
        raise ValueError("Output directory must be new")
    # Research Parquet/CSVs must never become warehouse input partitions.
    resolved = output_dir.resolve()
    data_root = repo.store.data_dir.resolve()
    if resolved.is_relative_to(data_root) and not resolved.is_relative_to(data_root / "research"):
        raise ValueError("Output under data must be inside data/research")
    source_manifest = audit_dir / "run_manifest.json"
    signal_path = audit_dir / "a0_signals.parquet"
    core_path = Path(__file__).parents[1] / "strategy/huichun_a0.py"
    source_files = [
        Path(__file__),
        core_path,
        Path(__file__).with_name("huichun_audit.py"),
        Path(__file__).with_name("huichun_calendar.py"),
        Path(__file__).parents[1] / "backtest/huichun_daily_proxy.py",
        Path(__file__).parents[1] / "backtest/huichun_events.py",
        Path(__file__).parents[1] / "backtest/huichun_execution.py",
    ]
    paths = [
        source_manifest,
        signal_path,
        plan_path,
        calendar_path,
        suspensions_path,
        *source_files,
    ]
    if factors_path is not None:
        paths.append(factors_path)
    before = _research_snapshot(repo, paths)
    plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    source = json.loads(source_manifest.read_text(encoding="utf-8-sig"))
    start, end = date.fromisoformat(plan["start"]), date.fromisoformat(plan["end"])
    if (
        plan["version"] != 1
        or plan["experiment"] != "A0_DAILY_PROXY"
        or plan["hold_market_days"] != 10
        or plan["commission_rate"] != 0.0003
        or plan["minimum_commission"] != 5
        or plan["exit_models"] != ["fixed_10_days", "close_risk_or_10_days"]
        or plan["risk_exit"] != {"stop_loss": -0.07, "take_profit": 0.15, "below_ma60": True}
    ):
        raise ValueError("Unsupported frozen research plan")
    split_names = set()
    for window in plan["splits"]:
        if (
            not window["name"].replace("_", "").isalnum()
            or window["name"] == "full"
            or window["name"] in split_names
            or not start
            <= date.fromisoformat(window["start"])
            <= date.fromisoformat(window["end"])
            <= end
        ):
            raise ValueError("Invalid split")
        split_names.add(window["name"])
    if source["summary"]["start"] != str(start) or source["summary"]["end"] != str(end):
        raise ValueError("Plan range differs from source signal audit")
    previous_market = {
        r["path"]: r
        for r in source["inputs"]["files"]
        if not r["path"].startswith(("calendar:", "suspensions:", "factors:"))
    }
    if previous_market != {r["path"]: r for r in before["market"]["files"]}:
        raise ValueError("Market inputs changed since signal audit; rerun audit first")
    source_factors = [r for r in source["inputs"]["files"] if r["path"].startswith("factors:")]
    if factors_path is None:
        if source_factors:
            raise ValueError("Source audit requires the same replacement factors")
    elif len(source_factors) != 1 or source_factors[0]["sha256"] != _hash(factors_path):
        raise ValueError("Replacement factors differ from source signal audit")
    if _hash(core_path) != source["source_code_sha256"]["huichun_a0.py"]:
        raise ValueError("A0 core changed since source audit")
    audit_path = Path(__file__).with_name("huichun_audit.py")
    if _hash(audit_path) != source["source_code_sha256"].get("huichun_audit.py"):
        raise ValueError("Daily preparation code changed since source audit; rerun audit first")
    calendar = load_market_calendar(
        calendar_path, start=date.fromisoformat(source["summary"]["raw_first"]), end=end
    )
    signals = pl.read_parquet(signal_path)
    params = A0Params(**source["parameters"])
    daily, factors = load_research_daily(
        repo, signals, calendar, end, load_suspensions(suspensions_path), params, factors_path
    )
    validate_factor_prices(daily, factors)
    print(f"Loaded {signals.height} candidates, {daily.height} valid daily rows", flush=True)
    sessions = (
        repo.execute_all(
            "SELECT symbol,cast(datetime AS DATE),count(*) FROM kline_minute GROUP BY 1,2 ORDER BY 1,2"
        )
        if repo.execute_one(
            "SELECT count(*) FROM information_schema.tables WHERE table_name='kline_minute'"
        )[0]
        else []
    )
    minute_keys = {(r[0], r[1]) for r in sessions}
    day_index = {day: i for i, day in enumerate(calendar)}
    matching_entries = sum(
        day_index[r["r_date"]] + 1 < len(calendar)
        and (r["symbol"], calendar[day_index[r["r_date"]] + 1]) in minute_keys
        for r in signals.iter_rows(named=True)
    )
    minute_summary = {
        "rows": sum(r[2] for r in sessions),
        "stock_days": len(sessions),
        "symbols": sorted({r[0] for r in sessions}),
        "raw_basis_verified": before["minute_raw_basis"] is not None,
        "matching_a0_entry_stock_days": matching_entries,
        "a1_b1_return_status": "unavailable_minute_contract_and_coverage",
    }
    # Only publish after all runs and the after-snapshot check complete.
    runs, event_results = {}, {}
    windows = [{"name": "full", "start": str(start), "end": str(end)}, *plan["splits"]]
    for window in windows:
        lo, hi = date.fromisoformat(window["start"]), date.fromisoformat(window["end"])
        selected = signals.filter(pl.col("r_date").is_between(lo, hi))
        events = event_study(
            selected, daily, calendar, end=hi, horizons=tuple(plan["event_horizons"])
        )
        event_results[window["name"]] = (events, summarize_events(events))
        for risk in (False, True):
            for bps in plan["slippage_bps"]:
                key = f"{window['name']}_{'risk' if risk else 'fixed'}_{bps}bps"
                runs[key] = simulate_daily_proxy(
                    signals,
                    daily,
                    calendar,
                    start=lo,
                    end=hi,
                    adjustments=factors,
                    slippage_bps=bps,
                    risk_exit=risk,
                    initial_cash=plan["initial_cash"],
                    max_positions=plan["max_positions"],
                    target_fraction=plan["target_equity_fraction"],
                    hold_days=plan["hold_market_days"],
                )
                print(f"Completed {key}", flush=True)
    after = _research_snapshot(repo, paths)
    if before != after:
        raise RuntimeError("Inputs changed during research; no report published")
    output_dir.mkdir(parents=True)
    summaries = []
    for key, result in runs.items():
        directory = output_dir / key
        directory.mkdir()
        summaries.append({"run": key, **result["stats"]})
        for table, empty_columns in {
            "equity": ("date", "equity"),
            "orders": ("symbol", "signal_date", "status"),
            "trades": ("symbol", "entry_date", "exit_date", "return_pct"),
            "open_positions": ("symbol", "entry_date", "units"),
        }.items():
            _write_rows(directory / f"{table}.csv", result[table], empty_columns)
        (directory / "stats.json").write_text(
            json.dumps(result["stats"], indent=2, ensure_ascii=False), encoding="utf-8"
        )
    event_summaries = []
    for key, (events, summary) in event_results.items():
        events.write_csv(output_dir / f"events_{key}.csv", include_bom=True)
        event_summaries.extend({"window": key, **row} for row in summary)
    _write_rows(output_dir / "event_summary.csv", event_summaries, ("window", "horizon"))
    _write_rows(
        output_dir / "portfolio_summary.csv",
        [
            {k: json.dumps(v, sort_keys=True) if isinstance(v, dict) else v for k, v in row.items()}
            for row in summaries
        ],
        ("run", "net_return"),
    )
    manifest = {
        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "experiment": "A0_DAILY_PROXY",
        "factor_source": str(factors_path.resolve()) if factors_path else "repository_adj_factor",
        "plan": plan,
        "inputs": before,
        "source_code_sha256": {p.name: _hash(p) for p in source_files},
        "minute_coverage": minute_summary,
        "portfolio_runs": summaries,
        "event_summaries": event_summaries,
        "scope": "Available-data research with explicit assumptions; not original minute strategy returns",
    }
    (output_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (output_dir / "frozen_plan.json").write_bytes(plan_path.read_bytes())
    return {
        "output": str(output_dir.resolve()),
        "minute_coverage": minute_summary,
        "portfolios": summaries,
        "event_summaries": event_summaries,
    }
