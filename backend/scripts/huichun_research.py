"""Run frozen Huichun available-data research without downloads or live trading."""

import argparse
import json
from pathlib import Path

from app.services.huichun_research import run_research
from app.tickflow.repository import DataStore, KlineRepository


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", type=Path, default=Path(__file__).resolve().parents[2] / "data"
    )
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--suspensions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--factors", type=Path, help="Use the same replacement factors as the audit"
    )
    args = parser.parse_args()
    if not (args.data_dir / "kline_daily").is_dir():
        parser.error("Existing daily data required")
    store = DataStore(args.data_dir)
    try:
        result = run_research(
            KlineRepository(store),
            audit_dir=args.audit_dir,
            plan_path=args.plan,
            calendar_path=args.calendar,
            suspensions_path=args.suspensions,
            output_dir=args.output,
            factors_path=args.factors,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
