"""Run with: uv run python -m scripts.replay_market_game --start 2023-01-01 --output result.json"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from app.config import settings
from app.services.market_game_replay import run_replay
from app.tickflow.repository import DataStore, KlineRepository


def main() -> None:
    parser = argparse.ArgumentParser(description='超短线候选个股层离线回放（当前成分，研究用途，不生成交易记录）')
    parser.add_argument('--data-dir', type=Path, default=settings.data_dir)
    parser.add_argument('--start', type=date.fromisoformat, default=date(2023, 1, 1))
    parser.add_argument('--end', type=date.fromisoformat, default=date.today())
    parser.add_argument('--split', type=date.fromisoformat, help='按该日期拆成 train/test 两段')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('输出文件已存在，请使用新文件名')
    store = DataStore(args.data_dir)
    try:
        report = run_replay(KlineRepository(store), start=args.start, end=args.end,
                            split=args.split, data_dir=args.data_dir)
    finally:
        store.db.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=1, allow_nan=False, default=str)
    groups = [row for row in report['groups'] if '|' not in row['group'] or '|train' in row['group'] or '|test' in row['group']]
    print(json.dumps(groups, ensure_ascii=False, indent=1, default=str))


if __name__ == '__main__':
    main()
