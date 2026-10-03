"""Run local Huichun A0 daily audit without downloads or trading."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.services.huichun_audit import run_audit
from app.tickflow.repository import DataStore, KlineRepository


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", type=Path, default=Path(__file__).resolve().parents[2] / "data"
    )
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument(
        "--suspensions", type=Path, help="JSON full-day suspension evidence with publication dates"
    )
    parser.add_argument("--calendar", type=Path, help="JSON independent exchange holiday calendar")
    parser.add_argument(
        "--factors", type=Path, help="Research-only replacement event-factor Parquet"
    )
    args = parser.parse_args()
    if not (args.data_dir / "kline_daily").is_dir():
        parser.error("data-dir must contain existing daily data")
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")
    output = args.output or args.data_dir / "research" / "huichun" / stamp
    store = DataStore(args.data_dir)
    try:
        summary = run_audit(
            KlineRepository(store),
            start=args.start,
            end=args.end,
            output_dir=output,
            batch_size=args.batch_size,
            suspensions_path=args.suspensions,
            calendar_path=args.calendar,
            factors_path=args.factors,
        )
        print(
            json.dumps({"output": str(output.resolve()), **summary}, ensure_ascii=False, indent=2)
        )
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
