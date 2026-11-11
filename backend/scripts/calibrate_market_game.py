"""Run with: uv run python -m scripts.calibrate_market_game --output result.json"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from app.config import settings
from app.services.market_game_calibration import run_local_calibration
from app.services.market_game_credibility import CalibrationArchive


def main() -> None:
    parser = argparse.ArgumentParser(description='超短线规则样本内校准诊断，不生成交易回测')
    parser.add_argument('--data-dir', type=Path, default=settings.data_dir)
    parser.add_argument('--start', type=date.fromisoformat, default=date(2020, 1, 1))
    parser.add_argument('--end', type=date.fromisoformat)
    parser.add_argument('--split', type=date.fromisoformat,
                        help='按该日期把各组拆成 train/test 两段，只用于检查方向一致性')
    parser.add_argument('--output', type=Path, help='完整结果（含逐日记录）写入该新文件')
    parser.add_argument('--archive', action='store_true',
                        help='把可信度摘要追加归档到数据目录，供页面“规则可信度”只读展示；需同时指定 --split')
    args = parser.parse_args()
    if not args.output and not args.archive:
        parser.error('至少指定 --output 或 --archive')
    if args.archive and not args.split:
        parser.error('--archive 需要 --split，才能比较训练段与测试段方向')
    if args.output and args.output.exists():
        parser.error('输出文件已存在，请使用新文件名')
    report = run_local_calibration(args.data_dir, start=args.start, end=args.end, split=args.split)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
    if args.archive:
        print(f'已归档可信度摘要: {CalibrationArchive(args.data_dir).save(report)}')
    print(json.dumps({key: value for key, value in report.items() if key != 'observations'},
                     ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
