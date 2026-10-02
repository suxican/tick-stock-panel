"""Prepare an offline Fuyao cache repair, then apply a reviewed plan in small batches."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date
from pathlib import Path

import polars as pl

from app.services.daily_gap_repair import apply_repair, file_hash, prefix_ranges, prepare_repair
from app.tickflow.repository import DataStore, KlineRepository


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", type=Path, default=Path(__file__).resolve().parents[2] / "data"
    )
    sub = parser.add_subparsers(dest="action", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--coverage", type=Path, required=True)
    prepare.add_argument("--first-date", type=date.fromisoformat, required=True)
    prepare.add_argument("--floor", type=date.fromisoformat, required=True)
    prepare.add_argument("--factor-end", type=date.fromisoformat, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    apply = sub.add_parser("apply")
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--symbols", nargs="+")
    apply.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not (args.data_dir / "kline_daily").is_dir():
        parser.error("data-dir must contain daily data")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # All injected providers and the standard pipeline must use the explicit store.
    from app.config import settings

    settings.data_dir = args.data_dir.resolve()
    store = DataStore(settings.data_dir)
    try:
        repo = KlineRepository(store)
        if args.action == "prepare":
            from app.plugins.fuyao.provider import FuyaoProvider

            cache = settings.data_dir / "cache/fuyao"
            sources = sorted(cache.glob("*.parquet"))
            before = {str(p.relative_to(settings.data_dir)): file_hash(p) for p in sources}
            ranges = prefix_ranges(
                pl.read_parquet(args.coverage), first_date=args.first_date, floor=args.floor
            )
            provider = FuyaoProvider(cache_only=True)
            try:
                result = prepare_repair(
                    repo, provider, ranges, args.output, factor_end=args.factor_end
                )
            finally:
                provider.close()
            if before != {str(p.relative_to(settings.data_dir)): file_hash(p) for p in sources}:
                result["status"] = "invalid_sources_changed"
                (args.output / "plan.json").write_text(
                    json.dumps(result, default=str), encoding="utf-8"
                )
                raise RuntimeError("Source cache changed during preparation; do not apply plan")
            (args.output / "sources.json").write_text(
                json.dumps(before, indent=2), encoding="utf-8"
            )
        else:
            result = apply_repair(repo, args.plan, args.output, symbols=args.symbols)
        print(
            json.dumps(
                {k: v for k, v in result.items() if k not in {"files", "symbols"}},
                default=str,
                ensure_ascii=False,
                indent=2,
            )
        )
        print(f"symbols={len(result['symbols'])}")
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
