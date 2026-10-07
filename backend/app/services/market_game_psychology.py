"""Causal market-behaviour proxies, not measurements of investor identity.

Each feature is ranked against at most 252 earlier snapshot sessions. A score
requires 60 valid earlier observations per contributing feature and at least
two of three features. No future date or current-day observation enters its
own reference distribution. All weights and cutoffs remain research choices.
"""
# ruff: noqa: RUF001
from __future__ import annotations

import math
from collections import deque
from datetime import date

PSYCHOLOGY_VERSION = "1.0.0"
MIN_SAMPLES = 60
WINDOW = 252

# sign determines which historical tail means more of this dimension. Raw
# values remain visible; a reversed percentile does not change their units.
SPECS = (
    ("risk_appetite", "风险偏好", (
        ("breadth_up", "上涨占比", "ratio", 1),
        ("seal_rate", "封板率", "ratio", 1),
        ("previous_limit_premium", "昨日涨停股溢价", "return", 1),
    )),
    ("panic_pressure", "恐慌压力", (
        ("limit_down_ratio", "跌停占比", "ratio", 1),
        ("median_return", "市场中位收益（反向）", "return", -1),
        ("breadth_up", "上涨占比（反向）", "ratio", -1),
    )),
    ("profit_pressure", "兑现压力", (
        ("previous_limit_premium", "昨日涨停股溢价（反向）", "return", -1),
        ("seal_rate", "封板率（反向）", "ratio", -1),
        ("median_reversal", "中位收益较前日回落", "return", 1),
    )),
    ("repair_support", "修复承接", (
        ("breadth_change", "上涨占比较前日增加", "ratio", 1),
        ("median_change", "中位收益较前日改善", "return", 1),
        ("limit_down_relief", "跌停占比较前日收缩", "ratio", 1),
    )),
)

NARRATIVES = {
    "risk_appetite": (
        "短线参与者承担波动的意愿可能较强", "短线参与者可能趋于谨慎",
        "指数权重变化、事件刺激或机构再平衡，也能形成同样的广度与溢价",
        ["上涨广度、强势股溢价与封板同步维持，且亏钱效应未扩散"],
        ["广度回落且昨日强势股溢价转负，撤销风险偏好改善假设"],
    ),
    "panic_pressure": (
        "急于退出和损失厌恶可能放大卖压", "市场整体急迫卖出迹象相对较少",
        "系统性事件、流动性变化或集中调仓，也可能造成普跌，并非散户恐慌的直接证据",
        ["跌停占比上升与中位收益恶化持续，卖压扩散到更多股票"],
        ["跌停占比持续收缩且广度改善，撤销恐慌继续扩散假设"],
    ),
    "profit_pressure": (
        "前期强势参与者兑现或追涨者止损的压力可能上升", "强势股负反馈暂不突出",
        "缺少账户成本，无法区分获利卖出与止损；消息重估也能造成强势股回落",
        ["昨日强势股负溢价和封板弱化持续，新增承接不足"],
        ["强势股溢价与封板恢复，撤销兑现压力持续假设"],
    ),
    "repair_support": (
        "退出压力减轻后，短线信心和承接意愿可能恢复", "广度和卖压尚未共同出现明显修复",
        "一次反弹也可能来自回补或权重扰动，不能确认真实买方身份或趋势反转",
        ["下一交易日广度改善与跌停收缩共同延续，板块同步而非孤立反弹"],
        ["跌停重新扩散或广度再度走弱，撤销修复延续假设"],
    ),
}


def _number(value, ratio=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result) or (ratio and not 0 <= result <= 1):
        return None
    return result


def _covered(row: dict) -> bool:
    coverage = _number(row.get("coverage"), ratio=True)
    return coverage is not None and coverage >= 0.5


def _features(row: dict, previous: dict, calendar_verified: bool) -> dict:
    result = {key: _number(row.get(key), key in {"breadth_up", "seal_rate", "limit_down_ratio"})
              for key in ("breadth_up", "seal_rate", "limit_down_ratio", "median_return", "previous_limit_premium")}
    for name, source, sign in (("breadth_change", "breadth_up", 1),
                               ("median_change", "median_return", 1),
                               ("median_reversal", "median_return", -1),
                               ("limit_down_relief", "limit_down_ratio", -1)):
        prior = (_number(previous.get(source), source != "median_return")
                 if calendar_verified and _covered(row) and _covered(previous) else None)
        current = result[source]
        result[name] = None if current is None or prior is None else (current - prior) * sign
    return result


def _percentile(value, baseline: deque, sign: int):
    samples = [item for item in baseline if item is not None]
    if value is None or len(samples) < MIN_SAMPLES:
        return None, len(samples)
    equal = sum(math.isclose(item, value, rel_tol=0, abs_tol=1e-12) for item in samples)
    below = sum(item < value and not math.isclose(item, value, rel_tol=0, abs_tol=1e-12) for item in samples)
    rank = 100 * (below + equal / 2) / len(samples)
    return round(rank if sign > 0 else 100 - rank, 2), len(samples)


def _timeline(snapshot: dict) -> list[dict]:
    try:
        as_of = date.fromisoformat(snapshot["as_of"]).isoformat()
    except (KeyError, TypeError, ValueError):
        return []
    unique, conflicted = {}, set()
    for row in snapshot.get("history") or []:
        if not isinstance(row, dict):
            continue
        try:
            day = date.fromisoformat(row.get("date", "")).isoformat()
        except (TypeError, ValueError):
            continue
        if day >= as_of:
            continue
        if day in unique and unique[day] != row:
            conflicted.add(day)
        unique[day] = row
    # Conflicting duplicates retain a gap instead of silently choosing a source.
    rows = [({"date": day} if day in conflicted else unique[day]) for day in sorted(unique)]
    rows.append({**(snapshot.get("metrics") or {}), "date": as_of})
    return rows[-280:]


def analyze_psychology(snapshot: dict) -> dict:
    """Build bounded, deterministic profiles from facts visible at each date."""
    timeline = _timeline(snapshot)
    calendar_verified = (snapshot.get("calendar") or {}).get("verified") is True
    feature_ids = {item[0] for _, _, features in SPECS for item in features}
    baselines = {key: deque(maxlen=WINDOW) for key in feature_ids}
    history, masks, last_metrics, last_sizes = [], [], {}, {}
    previous = {}
    for row in timeline:
        values = _features(row, previous, calendar_verified)
        usable = calendar_verified and _covered(row)
        point, mask = {"date": row["date"]}, {}
        for key, _, specs in SPECS:
            metrics, sizes, contributing_sizes = [], [], []
            for feature, label, unit, sign in specs:
                rank, size = _percentile(values[feature] if usable else None, baselines[feature], sign)
                metrics.append({"id": feature, "label": label, "value": values[feature],
                                "unit": unit, "percentile": rank})
                if values[feature] is not None:
                    sizes.append(size)
                if rank is not None:
                    contributing_sizes.append(size)
            scores = [item["percentile"] for item in metrics if item["percentile"] is not None]
            point[key] = round(sum(scores) / len(scores), 2) if len(scores) >= 2 else None
            mask[key] = tuple(item["id"] for item in metrics if item["percentile"] is not None)
            effective_sizes = contributing_sizes if point[key] is not None else sizes
            last_metrics[key], last_sizes[key] = metrics, min(effective_sizes) if effective_sizes else 0
        history.append(point)
        masks.append(mask)
        for key in feature_ids:
            baselines[key].append(values[key] if usable else None)
        previous = row

    dimensions = []
    for key, label, specs in SPECS:
        metrics = last_metrics.get(key, [{"id": feature, "label": name, "value": None,
                                          "unit": unit, "percentile": None}
                                         for feature, name, unit, _ in specs])
        score = history[-1][key] if history else None
        prior = history[-2][key] if len(history) >= 2 else None
        comparable = len(masks) >= 2 and masks[-1][key] == masks[-2][key]
        delta = round(score - prior, 2) if score is not None and prior is not None and comparable else None
        coverage = sum(item["percentile"] is not None for item in metrics) / len(specs)
        level = "不可计算" if score is None else "偏高" if score >= 75 else "偏低" if score <= 25 else "中等"
        high, low, alternative, confirm, invalidation = NARRATIVES[key]
        facts = [f"{item['label']} {item['value']:.2%}" if item["value"] is not None
                 else f"{item['label']}缺失，未按零值处理" for item in metrics]
        sample_label = "参与评分的最短历史样本" if score is not None else "可用参考的最短历史样本"
        facts.append(f"可计算代理覆盖 {coverage:.0%}，{sample_label} {last_sizes.get(key, 0)} 个")
        if delta is not None:
            facts.append(f"较上一数据交易日变化 {delta:+.2f} 分；分差不等于价格涨跌预测")
        recent = [item[key] for item in history[-5:]]
        same_features = len(masks) >= 5 and all(item[key] == masks[-1][key] for item in masks[-5:])
        if len(recent) == 5 and all(item is not None for item in recent) and same_features:
            facts.append(f"最近5个数据交易日首尾变化 {recent[-1] - recent[0]:+.2f} 分，未跨缺失分数接续趋势")
        else:
            facts.append("近5日有效分数不足，短期趋势待确认")
        limitations = ["分值是方向调整后的历史分位等权均值，不是散户人数占比或上涨概率",
                       "量价无法辨认散户、机构或主力身份；心理表述只是可被推翻的假设"]
        if coverage < 1:
            limitations.append("存在缺项或不足60个历史参考样本；至少2项可计算才出分，缺项不补零")
        if score is not None and prior is not None and not comparable:
            limitations.append("相邻日可计算代理组成不同，不输出单日分差或接续短期趋势")
        interpretation = ("证据不足，暂不推断心理状态" if score is None else
                          (high if score >= 75 else low if score <= 25 else "代理指标处于历史中间区域，心理方向尚不鲜明"))
        if score is not None and coverage < 1:
            missing = "、".join(item["label"] for item in metrics if item["percentile"] is None)
            interpretation = (f"仅已覆盖代理处于历史{level}区域；{missing}缺少可用分位，"
                              f"整体{label}待确认，不能把局部分数视作完整市场结论")
            if key == "panic_pressure" and any(item["id"] == "limit_down_ratio" and item["percentile"] is None
                                               for item in metrics):
                interpretation += "；跌停尾部风险待确认"
            level = f"局部{level}"
        dimensions.append({
            "id": key, "label": label, "score": score, "delta": delta, "level": level,
            "sample_size": last_sizes.get(key, 0), "coverage": round(coverage, 6),
            "metrics": metrics, "facts": facts, "interpretation": interpretation,
            "alternative": alternative, "confirm": list(confirm), "invalidation": list(invalidation),
            "limitations": limitations,
        })
    return {
        "version": PSYCHOLOGY_VERSION, "scope": "market_proxy",
        "summary": "；".join(f"{row['label']}：{row['level']}" for row in dimensions),
        "dimensions": dimensions, "history": history,
        "limitations": (["独立交易日历未核验，仅展示原始事实，不给出分位、日变化或连续趋势"]
                        if not calendar_verified else []) + [
            "四维描述市场行为代理，不确认散户真实心理；不合成为重复计算同一行情的总分",
            "每一日只参考此前最多252个数据交易日；至少60个有效参考样本，当前日不进入自身基准",
            "历史分位不是预测概率，未进行样本外收益验证；分数不直接提高仓位",
            "维度覆盖指可计算代理占比；市场行情覆盖及历史成分时点可靠性另见数据证据",
            "市场行情覆盖缺失或低于50%的行不参与分位基准或日变化；覆盖分母本身的时点局限仍须复核",
            "数据交易日完整性以快照独立日历核验为准；缺口不按平盘处理，也不跨缺口计算日变化",
        ],
    }
