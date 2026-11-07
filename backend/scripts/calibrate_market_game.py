"""Run with: uv run python -m scripts.calibrate_market_game --output result.json"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from app.config import settings
from app.services.market_game_calibration import run_local_calibration


def main() -> None:
    parser = argparse.ArgumentParser(description='超短线规则样本内校准诊断，不生成交易回测')
    parser.add_argument('--data-dir', type=Path, default=settings.data_dir)
    parser.add_argument('--start', type=date.fromisoformat, default=date(2020, 1, 1))
    parser.add_argument('--end', type=date.fromisoformat)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('输出文件已存在，请使用新文件名')
    report = run_local_calibration(args.data_dir, start=args.start, end=args.end)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({key: value for key, value in report.items() if key != 'observations'},
                     ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
