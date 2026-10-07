"""Bounded first-board observations and chronological rule comparisons.

This is a signal study, not a fill simulator. Shapes use completed T-1 bars;
the T close is an observation anchor, never an assumed executable buy price.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, timedelta
from itertools import pairwise
from typing import Any

import polars as pl

from app.market_time import cn_today
from app.strategy.first_board import (
    FirstBoardRules,
    evaluate_candidates,
    filter_pattern_history,
    first_board_limit_price,
    required_history_bars,
)

SCHEMA_VERSION = 1
MAX_RESEARCH_DAYS = 366
MAX_SAMPLES = 500
MIN_TRAIN_SAMPLES = 30
MIN_VALIDATION_SAMPLES = 10
DAILY_COLUMNS = [
    "date", "symbol", "name", "open", "high", "low", "close", "raw_close", "raw_high",
    "raw_low", "raw_prev_close", "prev_close", "limit_up", "amount", "volume", "turnover_rate",
]

METHODOLOGY = (
    "使用 T-1 完成日线建立形态观察池, 记录 T 日触板、收盘封板与 T 收盘至下一交易日"
    "开盘/收盘的复权价格变化。收益观察不扣交易成本, 不假设买入成交, 不是交易胜率。"
)
LIMITATIONS = [
    "仅使用本地可得股票池; 若退市历史或历史风险警示名称缺失, 存在样本选择偏差。",
    "不使用当前概念归属重建历史题材, 不验证盘中主线、排队成交和回封先后。",
    "交易日参考来自本地上证指数日线; 无法据此证明交易所全日历完全覆盖。",
    "三类形态可能重叠, 同一股票同日按形态分别计样本; 这不是独立交易次数。",
]


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _validate_range(start: date, end: date, through: date | None) -> date:
    if type(start) is not date or type(end) is not date or start > end:
        raise ValueError("研究日期必须有效且起始日期不晚于结束日期")
    completed = cn_today() - timedelta(days=1)
    if through is not None:
        if type(through) is not date:
            raise ValueError("完成日必须是有效日期")
        completed = min(completed, through)
    if end > completed:
        raise ValueError("首板研究仅接受已完成日线, 结束日期必须早于北京时间今天")
    if (end - start).days + 1 > MAX_RESEARCH_DAYS:
        raise ValueError("单次首板研究最多 366 个自然日, 请缩小区间")
    return completed


def _load(repo, start: date, end: date, rules: FirstBoardRules,
          symbols: list[str] | None, through: date) -> tuple[pl.DataFrame, list[date], dict]:
    load_start = start - timedelta(days=required_history_bars(rules) * 3 + 30)
    load_end = min(through, end + timedelta(days=16))
    instruments = repo.get_instruments()
    universe = sorted(set(symbols)) if symbols is not None else (
        sorted(instruments["symbol"].drop_nulls().unique().to_list())
        if not instruments.is_empty() and "symbol" in instruments.columns else []
    )
    panel = repo.get_daily_batch(universe, load_start, load_end, columns=DAILY_COLUMNS) if universe else pl.DataFrame()
    metadata: dict = {
        "requested_start": str(start), "requested_end": str(end),
        "load_start": str(load_start), "load_end": str(load_end),
        "universe_count": len(universe), "name_source": "historical_rows",
        "session_source": "observed_stock_rows", "session_reference_available": False,
        "required_history_sessions": required_history_bars(rules) - 1,
    }
    if panel.is_empty():
        metadata.update(actual_start=None, actual_end=None, fingerprint=None)
        return panel, [], metadata
    if not {"symbol", "date"}.issubset(panel.columns):
        raise ValueError("首板研究日线缺少 symbol/date 字段")
    panel = panel.with_columns(pl.col("date").cast(pl.Date, strict=False)).filter(
        pl.col("symbol").is_in(universe)
        & pl.col("date").is_between(load_start, load_end)
    )
    panel = panel.select([column for column in DAILY_COLUMNS if column in panel.columns])
    if panel.select(pl.struct("symbol", "date").is_duplicated().any()).item():
        raise ValueError("首板研究日线存在重复股票交易日")
    if "name" not in panel.columns:
        if {"symbol", "name"}.issubset(instruments.columns):
            panel = panel.join(instruments.select("symbol", "name").unique("symbol"),
                               on="symbol", how="left")
            metadata["name_source"] = "current_instruments_snapshot"
        else:
            panel = panel.with_columns(pl.lit(None, dtype=pl.String).alias("name"))
            metadata["name_source"] = "missing"
    mandatory = {"symbol", "date", "name", "open", "high", "low", "close", "raw_close"}
    missing_columns = mandatory - set(panel.columns)
    if missing_columns:
        raise ValueError("首板历史研究缺少必要日线字段: " + ", ".join(sorted(missing_columns)))
    panel = panel.sort(["symbol", "date"])
    observed = sorted(panel["date"].drop_nulls().unique().to_list())
    sessions = observed
    get_index = getattr(repo, "get_index_daily", None)
    if callable(get_index):
        reference = get_index("000001.SH", load_start, load_end, columns=["date"])
        if reference is not None and not reference.is_empty() and "date" in reference.columns:
            index_days = sorted(reference["date"].cast(pl.Date, strict=False).drop_nulls().unique().to_list())
            index_days = [day for day in index_days if load_start <= day <= load_end]
            if index_days:
                sessions = sorted(set(index_days) | set(observed))
                metadata["session_source"] = "index_daily_and_observed_stock_rows"
                metadata["session_reference_available"] = True
    # Hash the actual numerical inputs, not file mtimes or mutable source identifiers.
    ordered = panel.select(sorted(panel.columns))
    header = str((pl.__version__, ordered.schema)).encode("utf-8")
    digest = hashlib.sha256(header + ordered.hash_rows(seed=0).to_numpy().tobytes()).hexdigest()
    metadata.update(actual_start=str(observed[0]) if observed else None,
                    actual_end=str(observed[-1]) if observed else None, fingerprint=digest)
    return panel, sessions, metadata


def _candidate_history(panel: pl.DataFrame, rules: FirstBoardRules) -> pl.DataFrame:
    # Each wrapper uses the same causal rolling features as build_candidates, but
    # computes the bounded history once per pattern rather than once per day.
    frames = [filter_pattern_history(panel, rules.model_dump(), pattern)
              for pattern in rules.enabled_patterns]
    frames = [frame for frame in frames if not frame.is_empty()]
    return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()


def _limit(candidate: dict, current: dict, day: date) -> float | None:
    price = _number(current.get("raw_close"))
    adjusted = _number(current.get("close"))
    reference = _number(current.get("raw_prev_close"))
    if reference is None and price and adjusted and price > 0 and adjusted > 0:
        previous = _number(candidate.get("reference_adjusted_close"))
        reference = previous * price / adjusted if previous and previous > 0 else None
    # Share the exact live-rule limit, including historical ChiNext rates and
    # correction of pre-reform 20% values cached by the generic enrichment path.
    return first_board_limit_price(
        candidate["symbol"], day, reference, explicit=_number(current.get("limit_up")),
    )


def _samples(panel: pl.DataFrame, sessions: list[date], start: date, end: date,
             rules: FirstBoardRules, *, outcome_end: date | None = None,
             candidate_history: pl.DataFrame | None = None,
             sample_limit: int = MAX_SAMPLES) -> tuple[list[dict], dict, dict, dict]:
    selected_days = [day for day in sessions if start <= day <= end]
    empty_coverage = {
        "sessions": [str(day) for day in selected_days], "session_count": len(selected_days),
        "candidate_sessions": 0, "missing_days": [], "missing_outcomes": 0,
        "missing_observations": 0, "history_gap_samples": 0,
        "history_sessions_before_start": sum(day < start for day in sessions),
        "history_sufficient": sum(day < start for day in sessions) >= required_history_bars(rules) - 1,
    }
    summary = _summary([])
    by_pattern = {pattern: _summary([]) for pattern in rules.enabled_patterns}
    if panel.is_empty():
        return [], empty_coverage, summary, by_pattern
    candidates = _candidate_history(panel, rules) if candidate_history is None else candidate_history
    partitions = {key[0]: frame for key, frame in panel.partition_by("date", as_dict=True).items()}
    missing = [day for day in selected_days if day not in partitions]
    empty_coverage["missing_days"] = [str(day) for day in missing]
    if candidates.is_empty():
        return [], empty_coverage, summary, by_pattern
    candidates = candidates.filter(pl.col("date").is_between(start, end))
    candidate_parts = {key[0]: frame for key, frame in candidates.partition_by("date", as_dict=True).items()}
    next_days = dict(pairwise(sessions))
    positions = {day: index for index, day in enumerate(sessions)}
    present = {day: set(frame["symbol"].to_list()) for day, frame in partitions.items()}
    samples = []
    for day in selected_days:
        candidate = candidate_parts.get(day)
        if candidate is None or day not in partitions:
            continue
        current = partitions[day]
        quotes = {row["symbol"]: row for row in current.to_dicts()}
        following = next_days.get(day)
        next_quotes = ({row["symbol"]: row for row in partitions[following].to_dicts()}
                       if following in partitions else {})
        required = required_history_bars(rules) - 1
        prior_days = sessions[max(0, positions[day] - required):positions[day]]
        day_samples = []
        observations = {(row["symbol"], row["pattern"]): row
                        for row in evaluate_candidates(candidate, current, rules, as_of=day)}
        for item in candidate.to_dicts():
            quote = quotes.get(item["symbol"], {})
            observation = observations.get((item["symbol"], item["pattern"]), {})
            gaps = []
            history_gap = len(prior_days) < required or any(
                item["symbol"] not in present.get(prior_day, set()) for prior_day in prior_days
            )
            if history_gap:
                gaps.append("missing_prior_session")
            raw_close, raw_high = _number(quote.get("raw_close")), _number(quote.get("raw_high"))
            limit = _limit(item, quote, day)
            if raw_close is None or raw_close <= 0:
                gaps.append("missing_raw_close")
            if raw_high is None or raw_high <= 0:
                gaps.append("missing_raw_high")
            if limit is None:
                gaps.append("missing_limit_reference")
            sealed = (abs(raw_close - limit) <= 0.005) if raw_close and limit else None
            touched = (raw_high >= limit - 0.005) if raw_high and limit else None
            if (raw_close and limit and raw_close > limit + 0.005) or (
                raw_high and limit and raw_high > limit + 0.005
            ) or (
                raw_close and raw_high and raw_close > raw_high + 0.005
            ):
                sealed = touched = None
                gaps.append("inconsistent_raw_price")
            next_quote = next_quotes.get(item["symbol"])
            if following is None or next_quote is None:
                gaps.append("missing_next_session")
            if following is not None and outcome_end is not None and following > outcome_end:
                next_quote = None
                gaps.append("outcome_beyond_split")
            close = _number(quote.get("close"))
            next_open = _number(next_quote.get("open")) if next_quote else None
            next_close = _number(next_quote.get("close")) if next_quote else None
            open_return = next_open / close - 1 if close and close > 0 and next_open and next_open > 0 else None
            close_return = next_close / close - 1 if close and close > 0 and next_close and next_close > 0 else None
            if open_return is None or close_return is None:
                gaps.append("missing_next_price")
            if history_gap:
                sealed = touched = open_return = close_return = None
            day_samples.append({
                "date": str(day), "symbol": item["symbol"], "name": item.get("name"),
                "pattern": item["pattern"], "pattern_label": item.get("pattern_label"),
                "state": "invalid" if history_gap else observation.get("state", "invalid"),
                "reference_date": str(item.get("reference_date") or ""),
                "observed_close": raw_close, "limit_up": limit, "touched": touched, "sealed": sealed,
                "next_session": str(following) if following else None,
                "next_open_return": open_return, "next_close_return": close_return,
                "gap_reasons": gaps, "reasons": observation.get("reasons", []),
                "evidence": observation.get("evidence", {}),
            })
        day_samples.sort(key=lambda row: (row["symbol"], row["pattern"]))
        samples.extend(day_samples[:max(0, sample_limit - len(samples))])
        _merge_summary(summary, _summary(day_samples))
        for pattern in rules.enabled_patterns:
            _merge_summary(by_pattern[pattern], _summary([row for row in day_samples if row["pattern"] == pattern]))
        empty_coverage["candidate_sessions"] += bool(day_samples)
        empty_coverage["missing_outcomes"] += sum(
            row["next_open_return"] is None or row["next_close_return"] is None for row in day_samples)
        empty_coverage["missing_observations"] += sum(
            row["sealed"] is None or row["touched"] is None for row in day_samples)
        empty_coverage["history_gap_samples"] += sum(
            "missing_prior_session" in row["gap_reasons"] for row in day_samples)
    return samples, empty_coverage, summary, by_pattern


def _summary(samples: list[dict]) -> dict:
    seals = [row["sealed"] for row in samples if row["sealed"] is not None]
    touches = [row["touched"] for row in samples if row["touched"] is not None]
    opens = [row["next_open_return"] for row in samples if row["next_open_return"] is not None]
    closes = [row["next_close_return"] for row in samples if row["next_close_return"] is not None]
    return {
        "candidates": len(samples), "unique_stock_days": len({(row["date"], row["symbol"]) for row in samples}),
        "touched": sum(touches), "touch_observed": len(touches),
        "touch_rate": sum(touches) / len(touches) if touches else None,
        "sealed": sum(seals), "seal_observed": len(seals),
        "seal_rate": sum(seals) / len(seals) if seals else None,
        "next_open_count": len(opens), "next_open_mean": sum(opens) / len(opens) if opens else None,
        "next_close_count": len(closes), "next_close_mean": sum(closes) / len(closes) if closes else None,
        "next_positive_rate": sum(value > 0 for value in closes) / len(closes) if closes else None,
    }


def _merge_summary(total: dict, part: dict) -> None:
    # Day-wise reduction keeps memory bounded; the display limit never changes denominators.
    weighted = {
        "touch_rate": "touch_observed", "seal_rate": "seal_observed",
        "next_open_mean": "next_open_count", "next_close_mean": "next_close_count",
        "next_positive_rate": "next_close_count",
    }
    for field, count in weighted.items():
        combined = total[count] + part[count]
        total[field] = ((total[field] or 0) * total[count] + (part[field] or 0) * part[count]) / combined if combined else None
    for field in total:
        if field not in weighted:
            total[field] += part[field]


def research_capabilities() -> dict:
    return {
        "daily_observation": {"available": True, "label": "收盘观察研究"},
        "minute_execution": {
            "available": False,
            "reason": "首板分钟实验尚未接入可核验的完整原价分钟、历史股本与成交时序; 不能降级为日线成交。",
        },
        "limit_queue_execution": {"available": False, "reason": "尚无逐笔委托及封板队列撮合"},
    }


def run_first_board_research(repo, *, start: date, end: date, rules: FirstBoardRules,
                             mode: str = "daily", symbols: list[str] | None = None,
                             through: date | None = None, sample_limit: int = MAX_SAMPLES) -> dict:
    completed = _validate_range(start, end, through)
    if mode not in {"daily", "daily_observation"}:
        raise ValueError("首板分钟成交研究尚未支持; 请使用日线收盘观察模式")
    if isinstance(sample_limit, bool) or not isinstance(sample_limit, int) or not 0 <= sample_limit <= MAX_SAMPLES:
        raise ValueError("研究样本展示上限必须在 0 至 500 之间")
    panel, sessions, window = _load(repo, start, end, rules, symbols, completed)
    samples, coverage, summary, by_pattern = _samples(panel, sessions, start, end, rules, sample_limit=sample_limit)
    return _json_safe({
        "schema_version": SCHEMA_VERSION, "mode": "daily_observation", "methodology": METHODOLOGY,
        "rules": rules.model_dump(), "data_window": window, "coverage": coverage,
        "summary": summary, "by_pattern": by_pattern,
        "samples": samples, "total_samples": summary["candidates"],
        "samples_truncated": summary["candidates"] > sample_limit,
        "limitations": LIMITATIONS, "capabilities": research_capabilities(),
    })


def _variants(rules: FirstBoardRules) -> list[tuple[str, str, FirstBoardRules]]:
    # Preserve the open interval at both floating-point boundaries. Decimal
    # display rounding can turn a valid small positive threshold into zero.
    deeper = min(
        math.nextafter(1.0, 0.0),
        rules.oversold_min_drawdown + min(0.05, (1 - rules.oversold_min_drawdown) / 2),
    )
    candidates = [
        ("baseline", "当前基线", {}),
        ("platform_tighter", "平台区间更窄", {"platform_max_range": rules.platform_max_range * 0.8}),
        ("trend_stronger", "趋势最低涨幅更高", {
            "trend_min_return": min(rules.trend_max_return, 1.0, round(rules.trend_min_return + 0.02, 8)),
        }),
        ("oversold_deeper", "超跌幅度更深", {"oversold_min_drawdown": deeper}),
    ]
    return [(identifier, label, FirstBoardRules.model_validate({**rules.model_dump(), **updates}))
            for identifier, label, updates in candidates]


def compare_first_board_rules(repo, *, start: date, end: date, rules: FirstBoardRules,
                              symbols: list[str] | None = None, through: date | None = None) -> dict:
    completed = _validate_range(start, end, through)
    panel, sessions, window = _load(repo, start, end, rules, symbols, completed)
    study_days = [day for day in sessions if start <= day <= end]
    cut = max(1, int(len(study_days) * 0.7)) if study_days else 0
    train_days, validation_days = study_days[:cut], study_days[cut:]
    variants = []
    for identifier, label, variant_rules in _variants(rules):
        candidates = _candidate_history(panel, variant_rules) if not panel.is_empty() else pl.DataFrame()
        # Outcome truncation purges T->T+1 labels crossing the train/test boundary.
        _, train_coverage, train_summary, _ = _samples(
            panel, sessions, train_days[0], train_days[-1], variant_rules, outcome_end=train_days[-1],
            candidate_history=candidates, sample_limit=0,
        ) if train_days else ([], {}, _summary([]), {})
        _, validation_coverage, validation_summary, _ = _samples(
            panel, sessions, validation_days[0], validation_days[-1], variant_rules,
            outcome_end=validation_days[-1], candidate_history=candidates, sample_limit=0,
        ) if validation_days else ([], {}, _summary([]), {})
        variants.append({
            "id": identifier, "label": label, "rules": variant_rules.model_dump(),
            "train": train_summary, "validation": validation_summary,
            "train_coverage": train_coverage, "validation_coverage": validation_coverage,
        })
    eligible = [item for item in variants if item["train"]["seal_observed"] >= MIN_TRAIN_SAMPLES]
    # Deterministic ties retain the baseline. Validation never participates in ranking.
    ranked = sorted(eligible, key=lambda item: -(item["train"]["seal_rate"] or 0))
    selected = ranked[0] if ranked else None
    baseline = variants[0]
    recommendation = None
    reason = "训练或验证样本不足, 仅展示观察统计, 不推荐更改规则"
    if selected and selected["validation"]["seal_observed"] >= MIN_VALIDATION_SAMPLES:
        if selected["id"] == "baseline":
            reason = "训练区间未发现优于当前基线的规则, 维持基线"
        elif (baseline["train"]["seal_observed"] >= MIN_TRAIN_SAMPLES
              and baseline["validation"]["seal_observed"] >= MIN_VALIDATION_SAMPLES):
            if (not window["session_reference_available"]
                    or any(item[coverage].get(gap)
                           for item in (baseline, selected)
                           for coverage in ("train_coverage", "validation_coverage")
                           for gap in ("missing_days", "missing_observations"))):
                reason = "样本存在行情缺口, 保留比较结果但不推荐更改规则"
            elif (selected["validation"]["seal_rate"] or 0) <= (baseline["validation"]["seal_rate"] or 0):
                reason = "训练选定规则未在留出区间优于基线, 不推荐更改规则"
            else:
                recommendation = {
                    "variant_id": selected["id"], "rules": selected["rules"],
                    "action": "review_only", "scope": "daily_observation",
                    "reason": "训练选定后在固定留出区间封板观察率高于基线; 需继续前向观察, 不是可成交收益改进证明",
                }
                reason = recommendation["reason"]
    experiment = {"rules": rules.model_dump(), "start": str(start), "end": str(end),
                  "data_fingerprint": window.get("fingerprint"), "version": SCHEMA_VERSION}
    return _json_safe({
        "schema_version": SCHEMA_VERSION, "mode": "daily_observation", "methodology": METHODOLOGY,
        "experiment_id": hashlib.sha256(json.dumps(experiment, sort_keys=True).encode()).hexdigest()[:20],
        "data_window": window, "selection_basis": "train_seal_rate", "split_fraction": 0.7,
        "train": {"sessions": [str(day) for day in train_days], "session_count": len(train_days)},
        "validation": {"sessions": [str(day) for day in validation_days], "session_count": len(validation_days)},
        "minimum_samples": {"train": MIN_TRAIN_SAMPLES, "validation": MIN_VALIDATION_SAMPLES},
        "variants": variants, "training_selected_id": selected["id"] if selected else None,
        "recommendation": recommendation, "recommendation_reason": reason, "auto_applied": False,
        "limitations": [*LIMITATIONS, "固定一次时间切分, 仅研究封板观察率; 不是分钟步进优化或交易收益优化。"],
    })
