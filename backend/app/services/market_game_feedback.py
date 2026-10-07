"""Freeze fallible expectations and inspect later, validated minute responses.

A falling price reinforced by selling is still reinforcing feedback. This
module never equates price direction with the sign of a feedback mechanism.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, time

from app.market_time import CN_TZ
from app.services.market_game_ecology_models import FeedbackHypothesis, FeedbackObservation

VERSION = "feedback-v1"
_LIMITATIONS = [
    "只观察原计划固定样本, 不代表全市场或完整板块, 也不能识别机构、量化账户或散户真实心理。",
    "心理分位是行为代理, 支持与否不是获利概率; 条件验证不构成入场授权。",
    "仅验证价格与行为的局部反馈; 融资、经营等基本面反馈未获验证。",
    "重复刷新相同分钟不是新增独立证据; 原假设保持冻结, 缺失不按中性处理。",
]


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _stamp(value):
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.astimezone(CN_TZ) if stamp.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _condition(metric, operator, value, description):
    return {"metric": metric, "operator": operator, "value": value, "description": description}


def freeze_feedback(report: dict) -> dict:
    """Freeze the rules before the listed future sessions, without reading them."""
    dimensions = {item.get("id"): _number(item.get("score")) for item in (report.get("psychology") or {}).get("dimensions", [])}
    appetite, panic = dimensions.get("risk_appetite"), dimensions.get("panic_pressure")
    cutoff = _stamp(report.get("cutoff"))
    validity = report.get("validity") or {}
    sessions = validity.get("observation_sessions", []) if validity.get("status") == "scheduled" and validity.get("calendar_verified") else []
    sessions = sorted(set(str(day) for day in sessions if cutoff is not None and str(day) > cutoff.date().isoformat()))[:3]
    kind, label = "unconfirmed", "反馈假设待确认"
    if panic is not None and panic >= 75:
        kind, label = "repair", "恐慌背景下的修复检验"
    elif appetite is not None and appetite >= 75:
        kind, label = "continuation", "高风险偏好下的延续检验"
    confirmation = [
        _condition("support", ">=", 75, "30分钟承接位置历史分位至少75"),
        _condition("distribution", "<=", 25, "30分钟回撤压力历史分位不高于25"),
        _condition("return_30m", ">", 0, "完整30分钟价格向上推进"),
        _condition("price_bias", ">=", 0, "处于分钟加权参考价上方"),
    ]
    if kind == "continuation":
        confirmation.insert(0, _condition("participation", ">=", 50, "参与强度历史分位至少50"))
        invalidation = [
            _condition("participation", ">=", 75, "参与强度历史分位至少75"),
            _condition("distribution", ">=", 75, "回撤压力历史分位至少75"),
            _condition("return_30m", "<=", 0, "完整30分钟价格未向上推进"),
        ]
    else:
        invalidation = [
            _condition("support", "<=", 25, "承接位置历史分位不高于25"),
            _condition("distribution", ">=", 75, "回撤压力历史分位至少75"),
            _condition("return_30m", "<", 0, "完整30分钟价格继续下跌"),
            _condition("price_bias", "<", 0, "低于分钟加权参考价"),
        ]
    if kind == "unconfirmed":
        confirmation, invalidation = [], []
    result = {
        "version": VERSION, "id": "", "kind": kind, "label": label,
        "as_of": str(report.get("as_of", "")), "cutoff": str(report.get("cutoff", "")),
        "observation_sessions": sessions, "expires_at": validity.get("expires_at") if sessions else None,
        "risk_appetite": appetite, "panic_pressure": panic, "minimum_samples": 60,
        "confirmation": confirmation, "invalidation": invalidation,
        "interpretation": "在冻结心理背景下检查后续承接与价格是否兑现条件; 不把模型预期视为市场共识。",
        "alternative": "消息重估、被动调仓或流动性变化也可产生相同响应, 不能据此确认行为主体。",
        "limitations": [*_LIMITATIONS, "仅在原计划已核验的未来观察交易日评估; 每组条件须同时满足。"],
    }
    result["id"] = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()[:24]
    return FeedbackHypothesis.model_validate(result).model_dump()


def _matches(conditions, values):
    comparisons = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
                   "<": lambda a, b: a < b, "<=": lambda a, b: a <= b}
    return bool(conditions) and all(values.get(item["metric"]) is not None
                                    and comparisons[item["operator"]](values[item["metric"]], item["value"])
                                    for item in conditions)


def observe_feedback(report: dict, behavior: dict, *, now: datetime) -> dict:
    """Validate a frozen hypothesis against completed, sufficiently sampled bars.

    ``behavior`` is the existing CapitalBehavior envelope; no provider access,
    resampling, identity inference or order/position mutation occurs here.
    """
    if now.tzinfo is None:
        raise ValueError("feedback observation requires an explicit timezone")
    now = now.astimezone(CN_TZ)
    frozen = report.get("feedback_hypothesis")
    hypothesis = FeedbackHypothesis.model_validate(frozen).model_dump() if frozen else None
    cutoff = _stamp(hypothesis["cutoff"]) if hypothesis else None
    report_cutoff = _stamp(report.get("cutoff"))
    created = _stamp(report.get("created_at"))
    envelope_time = _stamp(behavior.get("observed_at"))
    sessions = hypothesis["observation_sessions"] if hypothesis else []
    entry_open = _stamp(f"{sessions[0]}T{time(9, 30).isoformat()}+08:00") if sessions else None
    eligible = bool(hypothesis and cutoff is not None and cutoff == report_cutoff and cutoff < now
                    and created is not None and entry_open is not None
                    and cutoff.replace(microsecond=0) <= created < entry_open and created <= now
                    and hypothesis["as_of"] == report.get("as_of") and hypothesis["kind"] != "unconfirmed")
    results = []
    for row in behavior.get("rows", [])[:30]:
        observed = _stamp(row.get("as_of"))
        item = {"symbol": str(row.get("symbol", "")), "name": str(row.get("name", row.get("symbol", ""))),
                "observed_at": observed.isoformat() if observed else None, "status": "unavailable",
                "price_direction": "unknown", "feedback_type": "unclear", "stage": "unconfirmed",
                "label": "反馈待确认", "evidence": [], "reason": "冻结假设或后续分钟证据不足。"}
        results.append(item)
        if (not eligible or observed is None or observed > now or observed <= cutoff
                or observed.date() <= cutoff.date() or envelope_time is None or envelope_time > now
                or observed > envelope_time or observed.date().isoformat() != behavior.get("data_date")):
            continue
        if observed.date().isoformat() not in hypothesis["observation_sessions"]:
            expiry = _stamp(hypothesis.get("expires_at"))
            item.update(status="expired" if expiry and observed > expiry else "unavailable", reason="分钟日期不在冻结的观察窗口内。")
            continue
        if row.get("status") not in {"ready", "historical"} or behavior.get("status") not in {"ready", "historical", "limited"}:
            item["reason"] = "分钟质量未达到完整窗口与历史同时间样本要求。"
            continue
        values = {key: _number((row.get(key) or {}).get("score")) for key in ("participation", "support", "distribution")}
        windows = [window for window in row.get("windows", []) if window.get("minutes") == 30]
        values["return_30m"] = _number(windows[0].get("return")) if len(windows) == 1 else None
        values["price_bias"] = _number(row.get("price_bias"))
        if (any(value is None for value in values.values())
                or any((_number((row.get(key) or {}).get("sample_size")) or 0) < hypothesis["minimum_samples"] for key in ("participation", "support", "distribution"))
                or any(not 0 <= values[key] <= 100 for key in ("participation", "support", "distribution"))):
            item["reason"] = "完整30分钟、参考价或至少60个历史同时间样本缺失。"
            continue
        change = values["return_30m"]
        item.update(status="pending", price_direction="up" if change > 0 else "down" if change < 0 else "flat",
                    reason="证据已记录, 尚未同时满足冻结确认或失效条件。",
                    evidence=[f"完整30分钟涨跌{change:+.2%}; 承接分位{values['support']:.0f}, 回撤压力分位{values['distribution']:.0f}。"])
        if _matches(hypothesis["invalidation"], values):
            item.update(status="contradicted", label="原假设未兑现", reason="同时满足冻结的失效条件。", stage="fragile")
            if hypothesis["kind"] == "repair":
                item.update(feedback_type="reinforcing", stage="strengthening", label="下行自我加强候选")
        elif _matches(hypothesis["confirmation"], values):
            item.update(status="supported", reason="本次响应同时满足冻结确认条件; 仍须独立检查原计划入场及交易约束。")
            if hypothesis["kind"] == "repair":
                item.update(feedback_type="corrective", stage="repair", label="下跌后的自我矫正候选")
            else:
                item.update(feedback_type="reinforcing", stage="strengthening", label="上行自我加强候选")
    verified = [item for item in results if item["status"] in {"supported", "contradicted", "pending"}]
    supported = sum(item["status"] == "supported" for item in verified)
    contradicted = sum(item["status"] == "contradicted" for item in verified)
    return FeedbackObservation.model_validate({
        "version": VERSION, "hypothesis_id": hypothesis["id"] if hypothesis else None,
        "observed_at": max((item["observed_at"] for item in results if item["observed_at"]), default=None),
        "status": "available" if verified and len(verified) == len(results) else "limited" if verified else "unavailable",
        "summary": f"原计划固定样本: {supported}只响应支持假设, {contradicted}只触发失效条件; 其余待确认。" if verified else "尚无可用于验证冻结假设的完整后续分钟证据。",
        "rows": results, "limitations": list(_LIMITATIONS),
    }).model_dump()
