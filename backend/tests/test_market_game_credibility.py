import json

import pytest

from app.services.market_game_credibility import CalibrationArchive, summarize


def group(name, n, mean):
    return {'group': name, 'days': n, 'ew_1': {'n': n, 'mean': mean}}


def result(groups, split='2023-01-01'):
    return {'status': 'ready', 'scope': 'in_sample_market_proxy', 'rule_version': '1.4.0',
            'start': '2020-01-02', 'end': '2026-11-06', 'split': split, 'sentiment_days': 1500,
            'groups': groups, 'limitations': ['样本内研究']}


def rows_by_group(summary):
    return {row['group']: row for row in summary['rows']}


def test_verdicts_compare_signal_with_control_in_both_segments():
    groups = [
        group('gate:veto@train', 40, -.004), group('gate:none@train', 900, .001),
        group('gate:veto@test', 35, -.002), group('gate:none@test', 500, .0005),
        group('gate:caution@train', 60, .002), group('gate:caution@test', 50, -.001),
        group('shadow:volume_stall@train', 31, .001), group('shadow:volume_advance@train', 31, .001),
        group('shadow:volume_stall@test', 29, -.003), group('shadow:volume_advance@test', 80, .002),
    ]
    rows = rows_by_group(summarize(result(groups)))
    veto = rows['gate:veto']
    assert veto['verdict'] == 'consistent'
    assert veto['train']['diff'] == pytest.approx(-.005) and veto['test']['n'] == 35
    assert rows['gate:caution']['verdict'] == 'inconsistent'
    # 29 test days is below the floor even though the control is large.
    assert rows['shadow:volume_stall']['verdict'] == 'insufficient'
    assert rows['shadow:volume_stall']['kind'] == 'shadow' and rows['shadow:volume_stall']['card']
    # Absent groups are reported as insufficient, never as zero returns.
    assert rows['shadow:ease_down']['train']['mean'] is None
    assert rows['shadow:ease_down']['verdict'] == 'insufficient'


def test_zero_difference_is_not_counted_as_consistent():
    groups = [group(f'{name}@{seg}', 50, .001) for name in ('mode:crowded_exit', 'baseline')
              for seg in ('train', 'test')]
    assert rows_by_group(summarize(result(groups)))['mode:crowded_exit']['verdict'] == 'inconsistent'


def test_summary_requires_split():
    with pytest.raises(ValueError, match='split'):
        summarize(result([], split=None))


def test_archive_appends_and_reads_newest_valid(tmp_path):
    archive = CalibrationArchive(tmp_path)
    assert archive.latest() is None
    first = archive.save(result([]))
    second = archive.save(result([]))
    assert first != second
    root = tmp_path / 'user_data' / 'market_game' / 'calibration'
    assert sorted(p.name for p in root.iterdir()) == sorted([first, second])
    newest = max(first, second)
    (root / newest).write_text('{broken', encoding='utf-8')
    loaded = archive.latest()
    assert loaded['archive'] == min(first, second) and loaded['skipped_archives'] == 1
    assert loaded['metric'] == 'ew_1' and loaded['created_at']
    # Unknown schema and unrelated files are ignored rather than trusted.
    (root / 'cal_29991231T000000_ffffffff.json').write_text(json.dumps({'schema_version': 99, 'rows': []}), encoding='utf-8')
    (root / 'notes.json').write_text('{}', encoding='utf-8')
    assert archive.latest()['archive'] == min(first, second)


def test_archive_refuses_unsplit_results_without_writing(tmp_path):
    archive = CalibrationArchive(tmp_path)
    with pytest.raises(ValueError):
        archive.save(result([], split=None))
    assert not (tmp_path / 'user_data' / 'market_game' / 'calibration').exists()
