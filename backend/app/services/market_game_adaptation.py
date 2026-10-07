"""Descriptive mode health from mature simulated trades, never automatic sizing."""
# ruff: noqa: RUF001
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from statistics import mean

from app.market_time import cn_now
from app.services.market_game_execution import timestamp
from app.services.market_game_execution_models import AdaptationResult, ExecutionEvaluation

MIN_INDEPENDENT_DATES = 30


def _walk_forward(clusters):
    baseline, filtered, accepted = [], [], 0
    for index, current in enumerate(clusters):
        known = [item for item in clusters[:index] if item["available"] < current["entry"]]
        # An additional completed date cluster is embargoed, beyond removal of
        # overlapping holding windows. No future output chooses a past weight.
        training = known[:-1]
        if len(training) < MIN_INDEPENDENT_DATES:
            continue
        keep = mean(item["value"] for item in training[-10:]) >= 0
        baseline.append(current["value"])
        filtered.append(current["value"] if keep else 0)
        accepted += int(keep)
    fixed = mean(baseline) if baseline else None
    alternative = mean(filtered) if filtered else None
    return {"state": "shadow_only" if baseline else "insufficient", "training_min_dates": MIN_INDEPENDENT_DATES,
            "test_dates": len(baseline), "accepted_dates": accepted, "withheld_dates": len(baseline) - accepted,
            "fixed_mean_net_return": round(fixed, 8) if fixed is not None else None,
            "filtered_mean_net_return": round(alternative, 8) if alternative is not None else None,
            "difference": round(alternative - fixed, 8) if fixed is not None else None,
            "reason": "时间顺序滚动：至少30个已知非重叠日期簇，隔离最近一个已完成簇；此前10簇均值非负则保留，否则影子空仓。仅比较已退出样本，不是全策略收益证明。"}


def analyze_adaptation(evaluations: list[dict], *, now: datetime | None = None) -> dict:
    """Keep refreshes, symbols in a theme and overlapping windows from inflating n.

    Different rule/cost versions are never pooled. Every group uses at most one
    earliest-issued report for an entry date; later refreshes may fill missing
    outcomes but cannot manufacture additional training samples.
    """
    now = timestamp(now or cn_now())
    groups, excluded = defaultdict(list), defaultdict(int)
    cohorts, prepared = {}, []
    for raw in evaluations:
        try:
            raw = ExecutionEvaluation.model_validate(raw).model_dump(mode="json")
            if timestamp(raw["evaluated_at"]) > now:
                continue
            created = timestamp(raw["report_created_at"])
            prepared.append(raw)
            policy_key = (raw["rule_version"], json.dumps(raw["costs"], sort_keys=True))
            for row in raw.get("rows", []):
                day = next((label.get("trade_date") for label in row["labels"] if label["horizon"] == 1), None)
                if not day:
                    continue
                opened = timestamp(f"{day}T09:30:00+08:00")
                if created >= opened:
                    continue
                key = (row["mode"], row["regime"], opened.date(), *policy_key)
                identity = (created, raw["report_id"])
                if key not in cohorts or identity < cohorts[key]:
                    cohorts[key] = identity
        except (ValueError, KeyError, TypeError):
            continue
    seen = set()
    for raw in sorted(prepared, key=lambda item: (item.get("evaluated_at", ""), item.get("report_id", ""))):
        try:
            evaluation = ExecutionEvaluation.model_validate(raw).model_dump(mode="json")
            evaluated = timestamp(evaluation["evaluated_at"])
        except (ValueError, TypeError, KeyError):
            continue
        if evaluated > now:
            continue
        costs = json.dumps(evaluation["costs"], sort_keys=True)
        for row in evaluation["rows"]:
            for label in row["labels"]:
                key = (row["mode"], row["regime"], label["horizon"], evaluation["rule_version"], costs)
                groups.setdefault(key, [])
                if not row["entry_time"] or label["state"] != "exited" or not label["mature"] or label["net_return"] is None:
                    excluded[key] += 1
                    continue
                try:
                    entry = timestamp(row["entry_time"])
                    created = timestamp(evaluation["report_created_at"])
                    exited = timestamp(label["exit_time"])
                    available = timestamp(label["outcome_available_at"])
                    selected = cohorts.get((row["mode"], row["regime"], entry.date(), evaluation["rule_version"], costs))
                    if (selected != (created, evaluation["report_id"]) or not created < entry < exited <= available <= evaluated
                            or available > now or exited.date() <= entry.date()):
                        excluded[key] += 1
                        continue
                except (ValueError, TypeError, KeyError):
                    excluded[key] += 1
                    continue
                identity = (row["symbol"], entry.date(), *key)
                if identity in seen:
                    excluded[key] += 1
                    continue
                seen.add(identity)
                # Market maturity is not the same as actual discovery. A label
                # first reconstructed months later was not available to an
                # earlier fold, even when its exit minute is historical.
                groups[key].append({"entry": entry, "exit": exited, "available": max(available, evaluated),
                                    "theme": row["sector"], "value": label["net_return"]})
    output = []
    for key, samples in groups.items():
        mode, regime, horizon, version, _ = key
        # Equal-weight date/theme clusters: multiple same-theme stocks do not
        # count as independent successes. Dates are the inferential sample unit.
        clustered = defaultdict(list)
        for sample in samples:
            clustered[(sample["entry"].date(), sample["theme"])].append(sample)
        dates = defaultdict(list)
        for (day, _), values in clustered.items():
            dates[day].append({"value": mean(item["value"] for item in values),
                               "exit": max(item["exit"].date() for item in values),
                               "entry": min(item["entry"] for item in values),
                               "available": max(item["available"] for item in values),
                               "count": len(values)})
        independent, last_exit, accepted = [], None, 0
        omitted = excluded[key]
        for day, themes in sorted(dates.items()):
            if last_exit is not None and day <= last_exit:
                omitted += sum(item["count"] for item in themes)
                continue
            independent.append({"value": mean(item["value"] for item in themes),
                                "entry": min(item["entry"] for item in themes),
                                "available": max(item["available"] for item in themes)})
            accepted += len(themes)
            omitted += sum(item["count"] for item in themes) - len(themes)
            last_exit = max(item["exit"] for item in themes)
        sufficient = len(independent) >= MIN_INDEPENDENT_DATES
        values = [item["value"] for item in independent]
        weakening = sufficient and mean(values[-10:]) < 0 and mean(values[-10:]) < mean(values[:-10])
        state = "insufficient" if not sufficient else "weakening" if weakening else "observing"
        output.append({"id": hashlib.sha256(json.dumps(key).encode()).hexdigest()[:16], "rule_version": version,
                       "mode": mode, "regime": regime, "horizon": horizon,
                       "sample_size": accepted, "independent_dates": len(independent), "excluded_count": omitted,
                       "state": state, "mean_net_return": round(mean(values), 8) if values else None,
                       "walk_forward": _walk_forward(independent),
                       "reason": (f"规则{version}；按交易日与题材聚类并剔除重叠持有窗口。"
                                  + (f"至少需要{MIN_INDEPENDENT_DATES}个不重叠交易日期，目前仅用于记录。" if not sufficient else
                                     "最近10个独立日期表现弱于此前且均值为负，仅提示衰减候选。" if weakening else
                                     "样本仅支持描述性观察，尚未通过样本外固定基准对照。"))})
    return AdaptationResult.model_validate({
        "evaluated_at": now.isoformat(timespec="seconds"), "groups": output,
        "limitations": [
            "只纳入评估时点之前已成熟且已模拟退出的扣费收益；观察名单涨跌幅、未成交、未退出和未来标签不进入均值。",
            "重复刷新及同股同入场日去重，同日同题材聚类，重叠持有窗口剔除；样本数不是独立交易次数。",
            "不同研究规则或成本配置分开统计；30个独立日期是描述性展示门槛，不代表已证明策略有效。",
            "滚动对照只用当时已知成熟样本，另隔离最近一个日期簇；该退出样本诊断尚不足以验证完整策略，始终影子观察，不自动调整仓位。",
            "训练可得时点取退出标签成熟时刻与首次归档评估时刻的较晚者，事后补算不会进入更早的滚动训练窗口。",
            "数据窗口来自有界归档读取，不保证覆盖全部历史；亏损无法退出的开放头寸另列，不把幸存退出样本当全策略收益。",
        ],
    }).model_dump(mode="json")
