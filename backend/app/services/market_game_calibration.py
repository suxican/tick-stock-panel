"""Offline, in-sample rule diagnostics. Never writes forecasts or trade records."""
from __future__ import annotations

import math
from collections import Counter
from datetime import date
from pathlib import Path

import polars as pl

from app.services.index_const import CORE_INDEX_SYMBOLS
from app.services.market_game import RULE_VERSION, _market_state, _metrics, _number
from app.services.market_game_sentiment import SentimentArchive, gate_series
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
        # Equal-weight session return; |r| > 35% exceeds every A-share limit
        # outside IPO windows and is treated as a data error, not a return.
        pl.col('_return').filter(pl.col('_return').abs() <= 0.35).mean().alias('ew_return'),
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
        # A closing high needs a complete lookback; any gap makes the flag null.
        (pl.col('close') >= pl.col('close').rolling_max(NEW_HIGH_WINDOW).over('symbol'))
        .cast(pl.Float64).alias('_new_high'),
    ).with_columns(
        # Highs in the sessions strictly before the signal day.
        pl.col('_new_high').shift(1).rolling_max(RECENT_HIGH_SESSIONS).over('symbol')
        .alias('index_recent_highs'),
    )
    keys = ['index_return', 'index_ret5', 'index_ret20', 'index_above_ma20']
    frame = frame.group_by('date').agg(*[
        pl.when(pl.col(key).count() == len(CORE_INDEX_SYMBOLS))
        .then(pl.col(key).mean()).alias(key) for key in keys
    ], pl.when(pl.col('index_recent_highs').count() == len(CORE_INDEX_SYMBOLS))
        .then(pl.col('index_recent_highs').sum()).alias('index_recent_highs'))
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
        supplement, index_row = premiums.get(day, {}), indices.get(day, {})
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
            **index_row,
        })
        # Shadow inputs stay outside _metrics: they must not change complete_inputs or coverage.
        rows.append({'date': day.isoformat(), **metrics, 'ew_return': _number(supplement.get('ew_return')),
                     'index_recent_highs': _number(index_row.get('index_recent_highs'), 0)})
        amounts.append(amount)
    return rows


# Shadow groups test Selden's qualitative claims with thresholds fixed before
# running; they never feed rules, reports or allocation. Each has a control so
# the comparison is signal versus a like-for-like day, not versus everything.
HEAT_AMOUNT_RATIO = 1.2  # same threshold as the rule's heat check
EASE_WINDOW = 20
EASE_MIN_DAYS = 5
EASE_DOWN_RATIO = .8
EASE_UP_RATIO = 1.25
DROP_THRESHOLD = -.015
DROP_LOOKBACK = 5
NEW_HIGH_WINDOW = 60
RECENT_HIGH_SESSIONS = 5
RECENT_HIGH_MIN_INDICES = 2

SHADOW_DEFINITIONS = [
    {'group': 'shadow:volume_stall', 'control': 'shadow:volume_advance', 'label': '放量滞涨',
     'card': 'C1-4 浮动供给雪球', 'definition':
     f'趋势上行且成交额倍数≥{HEAT_AMOUNT_RATIO}，中位涨跌幅≤0；对照组为同条件下中位涨跌幅>0'},
    {'group': 'shadow:ease_down', 'control': 'shadow:ease_up', 'label': '跌易涨难',
     'card': 'C7-4 识读技术位置', 'definition':
     f'趋势上行；近{EASE_WINDOW}个交易日上涨日与下跌日“|中位涨跌幅|÷成交额倍数”均值之比<{EASE_DOWN_RATIO}，'
     f'各方向至少{EASE_MIN_DAYS}天；对照组为该比值>{EASE_UP_RATIO}'},
    {'group': 'shadow:first_drop_after_high', 'control': 'shadow:drop_no_high', 'label': '新高后首次急跌',
     'card': 'C1-5 健康回调的陷阱', 'definition':
     f'核心四指数当日均值≤{DROP_THRESHOLD:.1%}且此前{DROP_LOOKBACK}日无同级别下跌，'
     f'此前{RECENT_HIGH_SESSIONS}日内至少{RECENT_HIGH_MIN_INDICES}只创{NEW_HIGH_WINDOW}日收盘新高；'
     '对照组为同样首次急跌但无指数新高'},
]


def _shadow_keys(rows: list[dict], index: int, trend: str) -> list[str]:
    """Only rows up to and including the signal day; missing inputs join neither side."""
    current, keys = rows[index], []
    ratio, median = current['amount_ratio'], current['median_return']
    if trend == '上行' and ratio is not None and median is not None and ratio >= HEAT_AMOUNT_RATIO:
        keys.append('shadow:volume_stall' if median <= 0 else 'shadow:volume_advance')
    if trend == '上行' and index + 1 >= EASE_WINDOW:
        up, down = [], []
        for row in rows[index + 1 - EASE_WINDOW:index + 1]:
            r, m = row['amount_ratio'], row['median_return']
            if r is None or m is None or r <= 0 or m == 0:
                continue
            (up if m > 0 else down).append(abs(m) / r)
        if len(up) >= EASE_MIN_DAYS and len(down) >= EASE_MIN_DAYS and sum(down) > 0:
            ease = (sum(up) / len(up)) / (sum(down) / len(down))
            if ease < EASE_DOWN_RATIO:
                keys.append('shadow:ease_down')
            elif ease > EASE_UP_RATIO:
                keys.append('shadow:ease_up')
    drop, highs = current['index_return'], current.get('index_recent_highs')
    prior = rows[max(0, index - DROP_LOOKBACK):index]
    if (drop is not None and drop <= DROP_THRESHOLD and highs is not None and len(prior) == DROP_LOOKBACK
            and all(row['index_return'] is not None and row['index_return'] > DROP_THRESHOLD for row in prior)):
        if highs >= RECENT_HIGH_MIN_INDICES:
            keys.append('shadow:first_drop_after_high')
        elif highs == 0:
            keys.append('shadow:drop_no_high')
    return keys


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _summary(values: list[float]) -> dict:
    return {'n': len(values), 'mean': sum(values) / len(values) if values else None,
            'positive_rate': sum(value > 0 for value in values) / len(values) if values else None,
            'p05': _quantile(values, .05), 'p10': _quantile(values, .10)}


# Outcome name -> (row field, horizon). Medians keep the original proxy; equal
# weight and next-day limit premium measure what short-term relay trades face.
_OUTCOMES = {
    'forward_1': ('median_return', 1), 'forward_3': ('median_return', 3),
    'ew_1': ('ew_return', 1), 'ew_3': ('ew_return', 3),
    'premium_1': ('previous_limit_premium', 1),
}


def calibrate_history(regime: pl.DataFrame, *, premiums: dict | None = None,
                      indices: dict | None = None, start: date | None = None,
                      end: date | None = None, sentiment: dict | None = None,
                      split: date | None = None) -> dict:
    if start and end and start > end:
        raise ValueError('起始日期不得晚于结束日期')
    if end and not regime.is_empty():
        regime = regime.filter(pl.col('date') <= end)
    rows = prepare_history(regime, premiums=premiums, indices=indices)
    gates = gate_series(sentiment) if sentiment else {}
    observations, coverage = [], Counter()
    group_days: dict[str, list[dict]] = {'baseline': []}
    for index, current in enumerate(rows):
        state, matches = _market_state(current, rows[index - 1] if index else _metrics({}))
        if start and current['date'] < start.isoformat():
            continue
        coverage.update(key for key, value in current.items() if key != 'date' and value is not None)
        outcomes = {}
        for name, (field, horizon) in _OUTCOMES.items():
            following = rows[index + 1:index + horizon + 1]
            valid = len(following) == horizon and all(r.get(field) is not None for r in following)
            outcomes[name] = math.prod(1 + r[field] for r in following) - 1 if valid else None
        gate_level = gates.get(current['date'], (None, 'unavailable'))[1] if sentiment else None
        keys = ['baseline', 'phase:' + state['phase']]
        if gate_level is not None:
            keys.append('gate:' + gate_level)
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
        # Mirrors 1.3.0: crowding is a prompt; a loss-effect veto blocks entry.
        entry_observable = (complete and (matches['panic_repair'] or matches['trend_pullback'])
                            and not state['risk_vetoes'] and gate_level != 'veto'
                            and state['phase'] not in {'退潮', '冰点'})
        if entry_observable:
            keys.append('entry_observable')
        shadow = _shadow_keys(rows, index, state['trend'])
        keys.extend(shadow)
        if split is not None:
            suffix = '@train' if current['date'] < split.isoformat() else '@test'
            keys.extend([key + suffix for key in keys])
        observation = {'date': current['date'], 'phase': state['phase'],
                       'matches': matches, 'complete_inputs': complete, 'gate': gate_level,
                       'entry_observable': entry_observable, 'shadow': shadow, **outcomes}
        observations.append(observation)
        for key in keys:
            group_days.setdefault(key, []).append(observation)
    groups = []
    for key, members in group_days.items():
        group = {'group': key, 'days': len(members)}
        for name in _OUTCOMES:
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
            'ew为次日全市场等权涨幅（剔除单日绝对涨跌超过35%的记录）；premium_1为次日昨日涨停股等权收盘涨幅，封板买不进，属于不可成交代理。',
            'gate组来自本地zzshare情绪归档的逐日回放，只与更早交易日比较分位；供应商历史可能事后修订，未获时点认证。',
            'split只按日期把各组拆成train/test两段，用于检查方向是否一致，不做阈值拟合。',
            'shadow组检验书中定性命题，阈值在运行前固定，只与各自对照组比较；不进入规则、报告或仓位。',
            '多组同时检验存在多重比较风险；单组方向一致不构成规则依据，需另行提升规则版本验证。',
        ],
        'shadow_definitions': SHADOW_DEFINITIONS,
        'split': split.isoformat() if split else None,
        'sentiment_days': len(sentiment or {}),
    }


def run_local_calibration(data_dir: Path, *, start: date, end: date | None = None,
                          split: date | None = None) -> dict:
    """Explicit offline batch; bounded projections of existing normalized stores."""
    regime = load_regime_history(data_dir)
    try:
        sentiment = SentimentArchive(data_dir).rows() or None
    except ValueError:
        sentiment = None
    if regime.is_empty():
        return calibrate_history(regime, start=start, end=end, sentiment=sentiment, split=split)
    if end:
        regime = regime.filter(pl.col('date') <= end)
    days = regime.sort('date')['date'].to_list()
    if not days:
        return calibrate_history(regime, start=start, end=end, sentiment=sentiment, split=split)

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
                             indices=index_metrics(index_daily, days), start=start, end=end,
                             sentiment=sentiment, split=split)
