"""Offline, in-sample rule diagnostics. Never writes forecasts or trade records."""
from __future__ import annotations

import math
from collections import Counter
from datetime import date
from pathlib import Path

import polars as pl

from app.services.index_const import CORE_INDEX_SYMBOLS
from app.services.market_game import RULE_VERSION, _market_state, _metrics, _number
from app.services.regime_builder import load_regime_history


def winner_premium(daily: pl.DataFrame, days: list[date]) -> dict:
    """Stored ladder flags are research inputs, not certified historical ST status."""
    required = {'date', 'symbol', 'close', 'consecutive_limit_ups'}
    if daily.is_empty() or not required <= set(daily.columns):
        return {}
    axis = pl.DataFrame({'date': days, '_session': range(len(days))})
    frame = daily.filter(~pl.struct('symbol', 'date').is_duplicated()).join(
        axis, on='date', how='inner',
    ).sort('symbol', 'date').with_columns(
        pl.col('close').shift(1).over('symbol').alias('_previous_close'),
        pl.col('consecutive_limit_ups').shift(1).over('symbol').alias('_previous_boards'),
        pl.col('_session').diff().over('symbol').alias('_gap'),
    ).with_columns(
        pl.when((pl.col('_gap') == 1) & (pl.col('close') > 0)
                & (pl.col('_previous_close') > 0) & pl.col('close').is_finite()
                & pl.col('_previous_close').is_finite())
        .then(pl.col('close') / pl.col('_previous_close') - 1).alias('_return'),
    ).group_by('date').agg(
        pl.col('_return').filter(pl.col('_previous_boards') >= 1)
        .mean().alias('previous_limit_premium'),
        pl.col('_return').count().alias('return_count'),
        pl.col('_return').filter(pl.col('_return') > 0).count().alias('up_count'),
    )
    return {row.pop('date'): row for row in frame.to_dicts()}


def index_metrics(indices: pl.DataFrame, days: list[date]) -> dict:
    if indices.is_empty() or not {'date', 'symbol', 'close'} <= set(indices.columns):
        return {}
    # Explicit calendar cross join makes missing sessions interrupt every window.
    axis = pl.DataFrame({'date': days}).join(
        pl.DataFrame({'symbol': list(CORE_INDEX_SYMBOLS)}), how='cross',
    )
    clean = indices.filter(~pl.struct('symbol', 'date').is_duplicated()).select(
        'date', 'symbol', pl.when(pl.col('close').is_finite() & (pl.col('close') > 0))
        .then(pl.col('close')).alias('close'),
    )
    frame = axis.join(clean, on=['date', 'symbol'], how='left').sort('symbol', 'date')
    expressions = []
    for window, key in ((1, 'index_return'), (5, 'index_ret5'), (20, 'index_ret20')):
        expressions.append(pl.when(
            pl.col('close').rolling_mean(window + 1).over('symbol').is_not_null(),
        ).then(pl.col('close') / pl.col('close').shift(window).over('symbol') - 1).alias(key))
    frame = frame.with_columns(*expressions,
        (pl.col('close') > pl.col('close').rolling_mean(20).over('symbol'))
        .cast(pl.Float64).alias('index_above_ma20'),
    )
    keys = ['index_return', 'index_ret5', 'index_ret20', 'index_above_ma20']
    frame = frame.group_by('date').agg(*[
        pl.when(pl.col(key).count() == len(CORE_INDEX_SYMBOLS))
        .then(pl.col(key).mean()).alias(key) for key in keys
    ])
    return {row.pop('date'): row for row in frame.to_dicts()}


def prepare_history(regime: pl.DataFrame, *, premiums: dict | None = None,
                    indices: dict | None = None) -> list[dict]:
    if regime.is_empty():
        return []
    if 'date' not in regime.columns or regime['date'].null_count():
        raise ValueError('环境历史缺少有效日期')
    if regime['date'].n_unique() != regime.height:
        raise ValueError('环境历史存在重复日期')
    premiums, indices = premiums or {}, indices or {}
    rows, amounts = [], []
    for row in regime.sort('date').to_dicts():
        day = row['date']
        if not isinstance(day, date):
            raise ValueError('环境历史日期类型无效')
        supplement = premiums.get(day, {})
        amount = _number(row.get('total_amount'), 0)
        average = _number(row.get('avg_turnover'), 0)
        # avg_turnover is regime_builder's mean amount, not a turnover ratio.
        # Keep numerator and denominator from the same regime population.
        # A partial supplemental daily store must never redefine market breadth.
        count = round(amount / average) if amount is not None and average else None
        up = _number(row.get('up_count'), 0)
        prior = amounts[-5:]
        amount_ratio = (amount / (sum(prior) / 5)
                        if amount is not None and len(prior) == 5
                        and all(value is not None for value in prior) and sum(prior) > 0 else None)
        metrics = _metrics({
            'breadth_up': up / count if up is not None and count else None,
            'above_ma20': row.get('above_ma20_pct'), 'median_return': row.get('median_pct'),
            'seal_rate': row.get('seal_rate'), 'limit_up': row.get('limit_up'),
            'limit_down': row.get('limit_down'), 'max_boards': row.get('max_consecutive'),
            'promotion_rate': row.get('promo_rate'), 'amount_ratio': amount_ratio,
            'previous_limit_premium': supplement.get('previous_limit_premium'),
            **indices.get(day, {}),
        })
        rows.append({'date': day.isoformat(), **metrics})
        amounts.append(amount)
    return rows


def _summary(values: list[float]) -> dict:
    return {'n': len(values), 'mean': sum(values) / len(values) if values else None,
            'positive_rate': sum(value > 0 for value in values) / len(values) if values else None}


def calibrate_history(regime: pl.DataFrame, *, premiums: dict | None = None,
                      indices: dict | None = None, start: date | None = None,
                      end: date | None = None) -> dict:
    if start and end and start > end:
        raise ValueError('起始日期不得晚于结束日期')
    if end and not regime.is_empty():
        regime = regime.filter(pl.col('date') <= end)
    rows = prepare_history(regime, premiums=premiums, indices=indices)
    observations, coverage = [], Counter()
    group_days: dict[str, list[dict]] = {'baseline': []}
    for index, current in enumerate(rows):
        state, matches = _market_state(current, rows[index - 1] if index else _metrics({}))
        if start and current['date'] < start.isoformat():
            continue
        coverage.update(key for key, value in current.items() if key != 'date' and value is not None)
        outcomes = {}
        for horizon in (1, 3):
            following = rows[index + 1:index + horizon + 1]
            valid = len(following) == horizon and all(r['median_return'] is not None for r in following)
            outcomes[f'forward_{horizon}'] = (
                math.prod(1 + r['median_return'] for r in following) - 1 if valid else None
            )
        keys = ['baseline', 'phase:' + state['phase']]
        keys.extend('mode:' + key for key in ('panic_repair', 'trend_pullback', 'crowded_exit') if matches[key])
        keys.extend('risk:' + key for key in ('short_index_risk', 'loss_veto') if matches[key])
        previous = rows[index - 1] if index else {}
        complete = all(current.get(key) is not None for key in _metrics({})) and all(
            previous.get(key) is not None for key in (
                'breadth_up', 'median_return', 'limit_down', 'previous_limit_premium',
                'seal_rate', 'max_boards', 'amount_ratio',
            )
        )
        if complete:
            keys.append('complete_inputs')
            keys.extend('complete_mode:' + key for key in ('panic_repair', 'trend_pullback') if matches[key])
        entry_observable = (complete and (matches['panic_repair'] or matches['trend_pullback'])
                            and not state['risk_vetoes'] and not matches['crowded_exit']
                            and state['phase'] not in {'退潮', '冰点'})
        if entry_observable:
            keys.append('entry_observable')
        observation = {'date': current['date'], 'phase': state['phase'],
                       'matches': matches, 'complete_inputs': complete,
                       'entry_observable': entry_observable, **outcomes}
        observations.append(observation)
        for key in keys:
            group_days.setdefault(key, []).append(observation)
    groups = []
    for key, members in group_days.items():
        group = {'group': key, 'days': len(members)}
        for horizon in (1, 3):
            name = f'forward_{horizon}'
            group[name] = _summary([row[name] for row in members if row[name] is not None])
        groups.append(group)
    return {
        'status': 'ready' if observations else 'unavailable', 'scope': 'in_sample_market_proxy',
        'rule_version': RULE_VERSION, 'start': observations[0]['date'] if observations else None,
        'end': observations[-1]['date'] if observations else None,
        'coverage': {key: coverage[key] for key in _metrics({})},
        'groups': groups, 'observations': observations,
        'limitations': [
            '样本内研究，未优化或自动替换阈值，不能证明样本外优势。',
            '收益是未来1/3个记录交易日的全市场日中位涨幅复合代理，不是候选股、组合或可成交收益。',
            '统计不含手续费和成交约束；不写入冻结报告、模型执行或适应性验证样本。',
            '复用环境历史与存储梯队的研究口径，历史ST状态、存续样本及事后数据修订未获时点认证。',
            '不消费历史phase标签；规则只读取当日及之前指标。窗口末尾及缺失收益不按零填补。',
            '交易序列以环境历史日期为轴；历史缺失交易日需先由原有同步入口补齐。',
            '各组包含重叠日期，3日窗口也重叠，不能当作独立试验或候选胜率。',
            'mode组只表示形态命中；complete_mode需完整输入，entry_observable另排除风险否决，不表示存在合格个股。',
            '完整输入要求当日全部规则指标及前日比较字段有效；指数统计要求核心四指数齐全，少于快照的部分指数研究样本。',
            '广度采用环境历史的上涨数与成交额/平均成交额推导样本数；溢价采用本地日线存储梯队，可能与环境历史的排除ST设置不同。',
        ],
    }


def run_local_calibration(data_dir: Path, *, start: date, end: date | None = None) -> dict:
    """Explicit offline batch; bounded projections of existing normalized stores."""
    regime = load_regime_history(data_dir)
    if regime.is_empty():
        return calibrate_history(regime, start=start, end=end)
    if end:
        regime = regime.filter(pl.col('date') <= end)
    days = regime.sort('date')['date'].to_list()
    if not days:
        return calibrate_history(regime, start=start, end=end)

    def read(directory: str, columns: list[str]) -> pl.DataFrame:
        paths = list((data_dir / directory).glob('**/*.parquet'))
        if not paths:
            return pl.DataFrame()
        frame = pl.scan_parquet(paths, missing_columns='insert', extra_columns='ignore')
        if not set(columns) <= set(frame.collect_schema().names()):
            return pl.DataFrame()
        return frame.filter(pl.col('date').is_between(days[0], days[-1])).select(columns).collect()

    daily = read('kline_daily_enriched', ['date', 'symbol', 'close', 'consecutive_limit_ups'])
    index_daily = read('kline_index_daily', ['date', 'symbol', 'close'])
    return calibrate_history(regime, premiums=winner_premium(daily, days),
                             indices=index_metrics(index_daily, days), start=start, end=end)
