"""Stage and apply bounded daily gaps through normalized provider/repository contracts.

Plans never write market data. Apply adds missing keys only, saves affected files,
verifies old values, and rebuilds the selected symbols through the existing pipeline.
Run maintenance with competing writers stopped; backups include a recovery journal.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

from app.services.huichun_audit import FACTOR_SCHEMA, RAW_COLUMNS
from app.tickflow.repository import KlineRepository

RAW_SCHEMA = {
    c: pl.String if c == "symbol" else pl.Date if c == "date" else pl.Float64 for c in RAW_COLUMNS
}


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _json(path: Path, value: dict) -> None:
    from app.services.fs_utils import atomic_write_text

    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _check_artifact_dir(root: Path, path: Path) -> None:
    root, path = root.resolve(), path.resolve()
    if path.is_relative_to(root) and not path.is_relative_to(root / "research"):
        raise ValueError("Repair artifacts inside data-dir must stay under research")


def prefix_ranges(coverage: pl.DataFrame, *, first_date: date, floor: date) -> pl.DataFrame:
    if floor >= first_date:
        raise ValueError("floor must precede first_date")
    return (
        coverage.filter(
            (pl.col("first_date") == first_date) & (pl.col("listing_date") < first_date)
        )
        .select(
            "symbol",
            pl.max_horizontal("listing_date", pl.lit(floor)).alias("start"),
            pl.lit(first_date - timedelta(days=1)).alias("end"),
        )
        .sort("symbol")
    )


def select_rows(frame: pl.DataFrame, ranges: pl.DataFrame) -> pl.DataFrame:
    frame = frame.select(RAW_COLUMNS).cast(RAW_SCHEMA)
    frame = (
        frame.join(ranges, on="symbol", how="inner")
        .filter(pl.col("date").is_between(pl.col("start"), pl.col("end")))
        .select(RAW_COLUMNS)
        .unique()
    )
    valid = (
        pl.all_horizontal(
            [
                pl.col(c).is_not_null() & pl.col(c).is_finite() & (pl.col(c) > 0)
                for c in RAW_COLUMNS[2:]
            ]
        )
        & (pl.col("high") >= pl.max_horizontal("open", "low", "close"))
        & (pl.col("low") <= pl.min_horizontal("open", "high", "close"))
    )
    if frame.filter(~valid.fill_null(False)).height:
        raise ValueError("Invalid daily rows; refusing repair")
    if frame.select(pl.struct("symbol", "date").is_duplicated().any()).item():
        raise ValueError("Conflicting duplicate daily keys")
    return frame.sort(["symbol", "date"])


def _factors(frame: pl.DataFrame) -> pl.DataFrame:
    if frame.is_empty():
        return pl.DataFrame(schema=FACTOR_SCHEMA)
    frame = frame.select(list(FACTOR_SCHEMA)).cast(FACTOR_SCHEMA).unique()
    invalid = frame.filter(
        pl.col("symbol").is_null()
        | pl.col("trade_date").is_null()
        | pl.col("ex_factor").is_null()
        | ~pl.col("ex_factor").is_finite()
        | (pl.col("ex_factor") <= 0)
    )
    if (
        invalid.height
        or frame.select(pl.struct("symbol", "trade_date").is_duplicated().any()).item()
    ):
        raise ValueError("Invalid or duplicate adjustment factors")
    return frame.sort(["symbol", "trade_date"])


def merge_factors(old: pl.DataFrame, incoming: pl.DataFrame) -> tuple[pl.DataFrame, int]:
    old, incoming = _factors(old), _factors(incoming)
    overlap = old.join(incoming, on=["symbol", "trade_date"], suffix="_new")
    if overlap.filter((pl.col("ex_factor") - pl.col("ex_factor_new")).abs() > 1e-10).height:
        raise ValueError("Existing adjustment factor conflict; review before applying")
    added = incoming.join(
        old.select("symbol", "trade_date"), on=["symbol", "trade_date"], how="anti"
    )
    return pl.concat([old, added]).sort(["symbol", "trade_date"]), added.height


def _read_raw(repo: KlineRepository, symbols: list[str]) -> pl.DataFrame:
    files = list((repo.store.data_dir / "kline_daily").rglob("*.parquet"))
    if not files:
        return pl.DataFrame(schema=RAW_SCHEMA)
    from app.parquet import scan_daily_parquet

    return (
        scan_daily_parquet(files)
        .filter(pl.col("symbol").is_in(symbols))
        .select(RAW_COLUMNS)
        .collect()
        .cast(RAW_SCHEMA)
    )


def prepare_repair(
    repo: KlineRepository, provider, ranges: pl.DataFrame, output: Path, *, factor_end: date
) -> dict:
    _check_artifact_dir(repo.store.data_dir, output)
    if output.exists() or ranges.is_empty():
        raise ValueError("Use a new plan directory and nonempty ranges")
    if (
        ranges["symbol"].n_unique() != ranges.height
        or ranges.filter(pl.col("start") > pl.col("end")).height
    ):
        raise ValueError("Invalid repair ranges")
    symbols = ranges["symbol"].to_list()
    start, end = ranges["start"].min(), ranges["end"].max()
    if factor_end < end:
        raise ValueError("Factor window must cover repaired history")
    before = _read_raw(repo, symbols)
    chunks = [
        df
        for df in provider.iter_daily(
            symbols,
            start_time=datetime.combine(start, datetime.min.time()),
            end_time=datetime.combine(end, datetime.min.time()),
        )
        if not df.is_empty()
    ]
    if not chunks:
        raise ValueError("No rows returned for repair")
    daily = select_rows(pl.concat(chunks, how="diagonal_relaxed"), ranges)
    absent = sorted(set(symbols) - set(daily["symbol"].to_list()))
    if absent:
        raise ValueError(f"No rows for requested symbols: {absent}")
    # Existing keys are never offered for overwrite, even if a vendor revised prices.
    daily = daily.join(before.select("symbol", "date"), on=["symbol", "date"], how="anti")
    factors = _factors(
        provider.get_adj_factors(
            symbols,
            start_time=datetime.combine(start, datetime.min.time()),
            end_time=datetime.combine(factor_end, datetime.min.time()),
        )
    ).filter(pl.col("symbol").is_in(symbols) & pl.col("trade_date").is_between(start, factor_end))
    factor_path = repo.store.data_dir / "adj_factor/all.parquet"
    old = (
        pl.read_parquet(factor_path) if factor_path.exists() else pl.DataFrame(schema=FACTOR_SCHEMA)
    )
    _, added_factors = merge_factors(old, factors)
    plan = ranges.join(
        daily.group_by("symbol").agg(
            pl.len().alias("daily_rows"),
            pl.col("date").min().alias("available_first"),
            pl.col("date").max().alias("available_last"),
        ),
        on="symbol",
        how="left",
    ).with_columns(pl.col("daily_rows").fill_null(0))
    output.mkdir(parents=True)
    daily.write_parquet(output / "daily.parquet")
    factors.write_parquet(output / "factors.parquet")
    plan.write_parquet(output / "ranges.parquet")
    plan.write_csv(output / "repair_plan.csv", include_bom=True)
    summary = {
        "status": "ready",
        "provider": getattr(provider, "name", "injected"),
        "data_dir": str(repo.store.data_dir.resolve()),
        "symbols": symbols,
        "added_daily_rows": daily.height,
        "added_factor_events": added_factors,
        "factor_end": factor_end,
        "factor_completeness": "cached_events_only_unverified",
        "files": {
            name: file_hash(output / name)
            for name in ("daily.parquet", "factors.parquet", "ranges.parquet")
        },
    }
    _json(output / "plan.json", summary)
    return summary


def recompute(repo: KlineRepository, symbols: list[str]) -> None:
    from app.indicators.pipeline import run_pipeline

    run_pipeline(data_dir=repo.store.data_dir, symbols=symbols, new_dates_only=False)
    repo.rebuild_views()
    repo.clear_cache()


def apply_repair(
    repo: KlineRepository, plan_dir: Path, output: Path, *, symbols: list[str] | None = None
) -> dict:
    from app.enriched_generation import enriched_publication_incomplete

    root = repo.store.data_dir.resolve()
    _check_artifact_dir(root, plan_dir)
    _check_artifact_dir(root, output)
    plan = json.loads((plan_dir / "plan.json").read_text(encoding="utf-8"))
    if plan.get("status") != "ready":
        raise ValueError("Plan is not ready")
    if Path(plan["data_dir"]).resolve() != root:
        raise ValueError("Plan belongs to another data directory")
    for name in ("daily.parquet", "factors.parquet", "ranges.parquet"):
        if file_hash(plan_dir / name) != plan["files"][name]:
            raise ValueError("Plan changed; prepare again")
    selected = sorted(set(symbols if symbols is not None else plan["symbols"]))
    if not selected or set(selected) - set(plan["symbols"]):
        raise ValueError("Symbols must be a nonempty subset of plan")
    if output.exists():
        raise ValueError("Use a new application directory")
    if enriched_publication_incomplete(root, "stock"):
        raise ValueError("Incomplete enriched publication; repair it before targeted maintenance")
    current = _read_raw(repo, selected)
    incoming = pl.read_parquet(plan_dir / "daily.parquet").filter(pl.col("symbol").is_in(selected))
    overlap = incoming.join(current, on=["symbol", "date"], suffix="_old")
    if overlap.filter(
        pl.any_horizontal([pl.col(c) != pl.col(c + "_old") for c in RAW_COLUMNS[2:]])
    ).height:
        raise ValueError("Daily keys changed since planning; refusing overwrite")
    added = incoming.join(current.select("symbol", "date"), on=["symbol", "date"], how="anti")
    factor_path = root / "adj_factor/all.parquet"
    old_factors = (
        pl.read_parquet(factor_path) if factor_path.exists() else pl.DataFrame(schema=FACTOR_SCHEMA)
    )
    new_factors = pl.read_parquet(plan_dir / "factors.parquet").filter(
        pl.col("symbol").is_in(selected)
    )
    factors, factor_count = merge_factors(old_factors, new_factors)
    result = {
        "status": "planned",
        "symbols": selected,
        "added_daily_rows": added.height,
        "added_factor_events": factor_count,
        "files": [],
    }
    output.mkdir(parents=True)
    journal = output / "result.json"
    _json(journal, result)
    # A previous attempt may have persisted raw/factors then failed before publishing
    # enriched. Re-running a plan must finish the rebuild even with zero new keys.
    daily_dates = sorted(added["date"].unique().to_list())
    enriched_dates = sorted(set(current["date"].to_list()) | set(daily_dates))
    paths = [root / "kline_daily" / f"date={d}" / "part.parquet" for d in daily_dates]
    paths += [root / "kline_daily_enriched" / f"date={d}" / "part.parquet" for d in enriched_dates]
    if factor_count:
        paths.append(factor_path)
    # Complete and verify backups before the first market-data write.
    for path in paths:
        if not path.resolve().is_relative_to(root):
            raise ValueError("Backup path escapes data directory")
        relative = path.relative_to(root)
        digest = file_hash(path) if path.exists() else None
        if digest is not None:
            backup = output / "backup" / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
            if file_hash(backup) != digest:
                raise RuntimeError("Backup changed during copy")
        result["files"].append({"path": relative.as_posix(), "before": digest})
    result["status"] = "backed_up"
    _json(journal, result)
    for item in result["files"]:
        path = root / item["path"]
        if (file_hash(path) if path.exists() else None) != item["before"]:
            raise RuntimeError("Data changed during backup; stop competing writers")
    try:
        result["status"] = "applying"
        _json(journal, result)
        repo.append_daily(added)
        if factor_count:
            factor_path.parent.mkdir(parents=True, exist_ok=True)
            repo._atomic_write_parquet(factors, factor_path)
        result["status"] = "recomputing"
        _json(journal, result)
        recompute(repo, selected)
        for item in result["files"]:
            path = root / item["path"]
            item["after"] = file_hash(path) if path.exists() else None
            if item["before"] is None:
                continue
            before = pl.read_parquet(output / "backup" / item["path"])
            after = pl.read_parquet(path)
            if item["path"].startswith("kline_daily_enriched/"):
                before = before.filter(~pl.col("symbol").is_in(selected))
                after = after.filter(~pl.col("symbol").is_in(selected))
            keys = ["symbol", "trade_date" if "trade_date" in before.columns else "date"]
            retained = after.join(before.select(keys), on=keys, how="semi").select(before.columns)
            if not before.sort(keys).equals(retained.sort(keys)):
                raise RuntimeError(f"Existing values changed: {item['path']}")
        after = _read_raw(repo, selected)
        if incoming.join(after, on=RAW_COLUMNS, how="anti").height:
            raise RuntimeError("Some planned rows did not persist")
        if after.height != current.height + added.height:
            raise RuntimeError("Unexpected daily row count")
        result["status"] = "complete"
        result["existing_values_preserved"] = True
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
        _json(journal, result)
        raise
    _json(journal, result)
    return result
