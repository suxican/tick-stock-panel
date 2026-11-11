"""Archived rule credibility: a summary of an explicit offline calibration run.

The API only reads the newest archive; it never runs calibration, which is a
bounded offline batch over local stores. Archives are append-only so earlier
conclusions stay auditable after rules or data change.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from uuid import uuid4

from app.market_time import cn_now
from app.services.fs_utils import atomic_write_text
from app.services.market_game_calibration import SHADOW_DEFINITIONS

SCHEMA_VERSION = 1
METRIC = 'ew_1'
MIN_DAYS = 30
_NAME = re.compile(r'cal_\d{8}T\d{6}_[0-9a-f]{8}\.json')

# Rule groups are compared against the comparable non-signal state, not every day.
_RULE_PAIRS = [
    {'group': 'gate:veto', 'control': 'gate:none', 'label': '亏钱门 · 暂停新增', 'kind': 'rule'},
    {'group': 'gate:caution', 'control': 'gate:none', 'label': '亏钱门 · 上限减半', 'kind': 'rule'},
    {'group': 'risk:short_index_risk', 'control': 'baseline', 'label': '指数短期风险否决', 'kind': 'rule'},
    {'group': 'mode:crowded_exit', 'control': 'baseline', 'label': '拥挤退潮提示', 'kind': 'rule'},
    {'group': 'entry_observable', 'control': 'baseline', 'label': '市场层可观察入场', 'kind': 'rule'},
]


def _segment(groups: dict, group: str, control: str, suffix: str) -> dict:
    signal, base = groups.get(group + suffix, {}).get(METRIC, {}), groups.get(control + suffix, {}).get(METRIC, {})
    n, control_n = signal.get('n', 0), base.get('n', 0)
    mean, control_mean = signal.get('mean'), base.get('mean')
    diff = mean - control_mean if mean is not None and control_mean is not None else None
    return {'n': n, 'mean': mean, 'control_n': control_n, 'control_mean': control_mean, 'diff': diff}


def _verdict(train: dict, test: dict) -> str:
    if min(train['n'], test['n'], train['control_n'], test['control_n']) < MIN_DAYS:
        return 'insufficient'
    if train['diff'] is None or test['diff'] is None:
        return 'insufficient'
    same = train['diff'] * test['diff'] > 0
    return 'consistent' if same else 'inconsistent'


def summarize(result: dict) -> dict:
    """Signal minus control on next-day equal-weight return, per train/test segment."""
    if not result.get('split'):
        raise ValueError('可信度摘要需要使用 --split 拆分训练段与测试段')
    groups = {row['group']: row for row in result.get('groups', [])}
    pairs = _RULE_PAIRS + [{'group': item['group'], 'control': item['control'], 'label': item['label'],
                            'kind': 'shadow', 'card': item['card'], 'definition': item['definition']}
                           for item in SHADOW_DEFINITIONS]
    rows = []
    for pair in pairs:
        train = _segment(groups, pair['group'], pair['control'], '@train')
        test = _segment(groups, pair['group'], pair['control'], '@test')
        rows.append({**pair, 'train': train, 'test': test, 'verdict': _verdict(train, test)})
    return {
        'schema_version': SCHEMA_VERSION, 'status': result.get('status'), 'metric': METRIC, 'min_days': MIN_DAYS,
        'scope': result.get('scope'), 'rule_version': result.get('rule_version'),
        'start': result.get('start'), 'end': result.get('end'), 'split': result.get('split'),
        'sentiment_days': result.get('sentiment_days'), 'rows': rows,
        'limitations': [
            *result.get('limitations', []),
            f'结论比较信号组与对照组的次日全市场等权涨幅之差；训练段与测试段各需至少{MIN_DAYS}天，同号才记为方向一致。',
            '方向一致只说明样本内两段同向，不是收益或胜率保证，也不会自动改变规则。',
        ],
    }


class CalibrationArchive:
    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir) / 'user_data' / 'market_game' / 'calibration'
        self._lock = threading.Lock()

    def save(self, result: dict) -> str:
        summary = summarize(result)
        created = cn_now()
        summary['created_at'] = created.isoformat()
        content = json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False)
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            name = f"cal_{created.strftime('%Y%m%dT%H%M%S')}_{uuid4().hex[:8]}.json"
            path = self.root / name
            if path.exists():  # never overwrite an earlier archive
                raise FileExistsError('校准归档已存在, 请重试')
            atomic_write_text(path, content)
        return name

    def latest(self) -> dict | None:
        """Newest readable archive; damaged files are skipped and counted, never repaired."""
        if not self.root.is_dir():
            return None
        names = sorted((p.name for p in self.root.iterdir() if _NAME.fullmatch(p.name)), reverse=True)
        skipped = 0
        for name in names:
            try:
                data = json.loads((self.root / name).read_text(encoding='utf-8'))
            except (OSError, ValueError):
                skipped += 1
                continue
            if not isinstance(data, dict) or data.get('schema_version') != SCHEMA_VERSION \
                    or not isinstance(data.get('rows'), list):
                skipped += 1
                continue
            return {**data, 'archive': name, 'skipped_archives': skipped}
        return None
