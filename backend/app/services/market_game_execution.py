"""Causal minute simulation of frozen conditions, isolated from watchlist returns.

The repository adapter supplies original-price minutes and independently checked
session limits. Missing evidence halts the simulation at its first gap. A later
defect cannot erase an earlier entry, and an unfilled entry never rolls forward.
"""
# ruff: noqa: RUF001
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from app.market_time import CN_TZ, cn_now
from app.services.market_game_execution_models import ExecutionEvaluation, ExecutionPolicy
from app.services.market_game_plan import candidate_conditions

VERSION = "1.0.0"
LIMITATIONS = [
    "仅模拟成交，未验证真实账户资金、委托队列或卖盘；不授权下单，不代表实际交易收益。",
    "全部信号使用已完成分钟，下一连续分钟开盘模拟成交；T+1、涨跌停、100股整数手和成本约束生效。",
    "模拟资金、费率、滑点和参与率是冻结研究假设；不构成经纪商收费或实际流动性的保证。",
    "下一分钟开盘仅是模拟价格基准，需等该分钟完成后验证量额与区间；分钟总量不能证明开盘瞬间存在足够卖盘。",
    "模拟均价仅由已归档连续竞价分钟量额累计；集合竞价是否包含在首分钟未知，不声称验证了原计划的真实全日均价。",
    "固定退出时刻为目标日14:55；此前已完成分钟触发结构失效则尝试下一分钟退出，买入当日仅记录退出需求。",
    "每个持有期限独立模拟退出；第1日受T+1限制，不生成可兑现净收益。",
    "分钟归档无法证明历史修订当时可得性；本结果须经前瞻存档与样本外验证后才能用于策略调整。",
]


def timestamp(value) -> datetime:
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("执行评估时点必须包含时区")
    return stamp.astimezone(CN_TZ)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def minute_slots(day: date) -> list[datetime]:
    # Normalized bars represent [start, start+1m); closing auction is excluded.
    return [datetime.combine(day, start, CN_TZ) + timedelta(minutes=i)
            for start, length in ((time(9, 30), 120), (time(13), 117)) for i in range(length)]


def _iso(value):
    return value.isoformat(timespec="seconds") if value else None


def _label(horizon, day, state, reason, **values):
    return {"horizon": horizon, "trade_date": day, "state": state, "reason": reason, **values}


def _condition_schema(candidate):
    expected = candidate_conditions(candidate)
    actual = candidate.get("conditions") or []
    fields = ("id", "phase", "scope", "metric", "operator", "value", "upper", "window", "minimum_samples")
    return len(actual) == len(expected) and {
        tuple(row.get(key) for key in fields) for row in actual
    } == {tuple(row.get(key) for key in fields) for row in expected}


def _fee(price, quantity, costs, *, sell=False):
    notional = price * quantity
    return (max(costs.minimum_commission, notional * costs.commission_rate)
            + notional * costs.transfer_rate + (notional * costs.sell_tax_rate if sell else 0))


def _bars(session, day, now):
    """Preserve the verified prefix instead of validating future rows eagerly."""
    lookup, duplicates = {}, set()
    for item in session.get("bars", []):
        try:
            stamp = timestamp(item["time"])
        except (KeyError, ValueError, TypeError):
            continue
        if stamp + timedelta(minutes=1) > now or stamp.date() != day:
            continue
        if stamp in lookup:
            duplicates.add(stamp)
        lookup[stamp] = item
    valid, reason = [], ""
    for stamp in minute_slots(day):
        if stamp + timedelta(minutes=1) > now:
            break
        if stamp not in lookup or stamp in duplicates:
            reason = "分钟缺失或重复，无法越过该时点确认触发顺序"
            break
        row = lookup[stamp]
        values = {key: _number(row.get(key)) for key in ("open", "high", "low", "close", "volume", "amount")}
        if any(value is None for value in values.values()):
            reason = "分钟价格或量额缺失"
            break
        op, high, low, close, volume, amount = (values[key] for key in values)
        if (min(op, high, low, close) <= 0 or high < max(op, close) or low > min(op, close)
                or low > high or min(volume, amount) < 0 or (volume == 0) != (amount == 0)
                or (volume > 0 and not low * .999 <= amount / (volume * 100) <= high * 1.001)):
            reason = "分钟OHLC、原始价格或量额标尺未通过校验"
            break
        valid.append({**values, "time": stamp})
    return valid, reason


def _day_evidence(session, day):
    if session.get("raw_scale_verified") is not True:
        return "原始价格标尺未核验"
    if session.get("corporate_action") is not False:
        return "除权或价格标尺变化，原计划不可跨标尺执行"
    if session.get("eligible") is not True:
        return "该交易日资格未核验"
    low, high = _number(session.get("limit_down")), _number(session.get("limit_up"))
    if low is None or high is None or not 0 < low < high:
        return "该交易日涨跌停边界未核验"
    try:
        if timestamp(session["reference_available_at"]) > datetime.combine(day, time(9, 30), CN_TZ):
            return "价格限制参考值晚于开盘才可得"
    except (ValueError, TypeError, KeyError):
        return "缺少价格限制参考时点"
    return ""


def _context(session, stamp):
    point = (session.get("contexts") or {}).get(_iso(stamp))
    if not point:
        return None, "缺少同分钟市场与完整板块复核"
    try:
        available = timestamp(point["available_at"])
        observed = timestamp(point["observed_at"])
        if observed != stamp or not stamp + timedelta(minutes=1) <= available <= stamp + timedelta(minutes=1):
            return None, "市场或板块证据不在对应信号时点可得"
    except (KeyError, ValueError, TypeError):
        return None, "市场与板块证据时点缺失"
    if point.get("market_coverage") != 1 or point.get("sector_coverage") != 1:
        return None, "全体冻结市场或板块成员未完整覆盖，不能以30只观察样本替代"
    breadth = _number(point.get("sector_breadth"))
    if (type(point.get("market_allowed")) is not bool or breadth is None or not 0 <= breadth <= 1):
        return None, "市场风险门或板块广度无法确认"
    if "retained_cap" in point and (_number(point["retained_cap"]) is None or not 0 <= point["retained_cap"] <= 1):
        return None, "追加资金观察约束无效"
    return point, ""


def _fill(bar, session, quantity, policy, *, sell=False):
    # A limit-priced open is rejected even if the minute later unlocked: the
    # archive does not reveal the queue or the time liquidity became available.
    raw = bar["open"]
    if not session["limit_down"] < raw < session["limit_up"]:
        return None
    multiplier = Decimal(1) + Decimal(str(policy.costs.slippage)) * (-1 if sell else 1)
    price = float((Decimal(str(raw)) * multiplier).quantize(Decimal("0.01"), rounding=ROUND_FLOOR if sell else ROUND_CEILING))
    if not session["limit_down"] < price < session["limit_up"]:
        return None
    if (bar["volume"] <= 0 or quantity > bar["volume"] * 100 * policy.costs.max_volume_participation
            or not bar["low"] <= price <= bar["high"]):
        return None
    return price


def _entry(candidate, session, day, now, policy):
    reason = _day_evidence(session, day)
    if reason:
        return None, "unavailable", reason
    bars, gap = _bars(session, day, now)
    if not bars:
        return None, "unavailable" if now > datetime.combine(day, time(9, 31), CN_TZ) else "pending", gap or "等待完整分钟"
    if bars[0]["open"] > candidate["trigger_high"]:
        return None, "no_entry", "开盘超过触发上限，取消后不因回落重启"
    volume = amount = 0.0
    held, pending = [], None
    for bar in bars:
        stamp = bar["time"]
        if pending and stamp == pending[0] + timedelta(minutes=1):
            price = _fill(bar, session, pending[1], policy)
            if price is not None and candidate["trigger_low"] <= price <= candidate["trigger_high"]:
                return {"signal": pending[0] + timedelta(minutes=1), "time": stamp,
                        "price": price, "quantity": pending[1]}, "entered", "冻结模拟条件在前一分钟成立，按下一连续分钟开盘模拟买入；真实账户及全日均价未核验"
        pending = None
        point, error = _context(session, stamp)
        if error:
            return None, "unavailable", error
        if (not point["market_allowed"] or point.get("capital_allowed") is False or point.get("retained_cap") == 0
                or point["sector_breadth"] < .4 or bar["low"] < candidate["invalidation_price"]):
            return None, "no_entry", "市场否决、板块转弱或结构失效，本入场日取消"
        volume += bar["volume"]
        amount += bar["amount"]
        if stamp.time() == time(13):
            held = []
        vwap = amount / (volume * 100) if volume else None
        held.append(vwap is not None and bar["close"] >= vwap)
        if (len(held) >= 3 and all(held[-3:]) and point["sector_breadth"] >= .6
                and candidate["trigger_low"] <= bar["close"] <= candidate["trigger_high"]):
            # Size against the frozen upper price so a later gap cannot exceed
            # the simulated budget. Entry fee is included in the cash bound.
            budget = policy.costs.account_equity * min(candidate["max_position"], point.get("retained_cap", candidate["max_position"]))
            unit = candidate["trigger_high"] * 100
            quantity = max(0, int((budget - policy.costs.minimum_commission) /
                                 (unit * (1 + policy.costs.commission_rate + policy.costs.transfer_rate)))) * 100
            minimum_order = 200 if candidate["symbol"].startswith(("688", "689")) else 100
            if quantity >= minimum_order:
                pending = stamp, quantity
    complete = now >= datetime.combine(day, time(15), CN_TZ)
    if gap:
        return None, "unavailable", gap
    return None, "no_entry" if complete else "pending", "入场观察日未模拟成交，不顺延" if complete else "尚未满足全部条件或等待下一完整分钟"


def _exit_label(candidate, entry, sessions, horizon, now, policy):
    target = sessions[horizon - 1][0]
    end = datetime.combine(target, time(15), CN_TZ)
    base = {"horizon": horizon, "trade_date": target.isoformat()}
    if horizon == 1:
        return {**base, "state": "not_executable" if now >= end else "pending", "mature": now >= end,
                "outcome_available_at": _iso(end) if now >= end else None,
                "reason": "买入当日受T+1限制，1日退出方案不可执行；该标签不代表后续仍然持仓"}
    stop_pending = False
    for day, session in sessions[:horizon]:
        if day > now.date():
            break
        reason = _day_evidence(session, day)
        if reason:
            return {**base, "state": "unavailable", "reason": reason}
        bars, gap = _bars(session, day, now)
        previous = None
        for bar in bars:
            stamp = bar["time"]
            if stamp < entry["time"]:
                continue
            exit_due = stop_pending or (day == target and stamp.time() >= time(14, 55))
            if day > entry["time"].date() and exit_due:
                price = _fill(bar, session, entry["quantity"], policy, sell=True)
                # Missing minutes terminate before reaching here; the first
                # executable open after a locked limit remains eligible.
                if price is not None:
                    purchase = entry["price"] * entry["quantity"] + _fee(entry["price"], entry["quantity"], policy.costs)
                    proceeds = price * entry["quantity"] - _fee(price, entry["quantity"], policy.costs, sell=True)
                    return {**base, "state": "exited", "mature": True,
                            "exit_time": _iso(stamp), "exit_price": price,
                            "net_return": round(proceeds / purchase - 1, 8),
                            "outcome_available_at": _iso(stamp + timedelta(minutes=1)),
                            "reason": "结构失效后下一可执行分钟退出" if stop_pending else "目标观察日14:55起首个可执行分钟退出"}
            # A low below the stop proves crossing, but not crossing order;
            # sell only at the next minute open, never retroactively at the stop.
            if bar["low"] < candidate["invalidation_price"]:
                stop_pending = True
            previous = stamp
        if gap:
            return {**base, "state": "unavailable", "reason": gap}
        if previous is None and now >= datetime.combine(day, time(9, 31), CN_TZ):
            return {**base, "state": "unavailable", "reason": "持有期间缺少分钟，无法验证退出顺序"}
    return {**base, "state": "open" if now >= end else "pending",
            "reason": "目标窗口结束仍未满足可成交退出条件；保留未退出，不编造收益" if now >= end else "持有窗口尚未结束"}


def simulate_executions(report: dict, evidence: dict, *, now: datetime | None = None) -> dict:
    """Pure engine. Evidence is normalized, original-price session data.

    ``evidence['sessions'][symbol][YYYY-MM-DD]`` contains bars, minute market/
    sector contexts, verified reference time, eligibility and daily limit prices.
    Evidence only grants simulation eligibility, never live authorization.
    """
    now = timestamp(now or cn_now())
    policy = ExecutionPolicy.model_validate(report.get("execution_policy") or {})
    validity = report.get("validity") or {}
    days = validity.get("observation_sessions") or []
    problem = evidence.get("error") or ""
    if not report.get("execution_policy"):
        problem = "旧报告未冻结执行假设，不能事后套用新成交规则"
    elif not validity.get("calendar_verified") or validity.get("status") != "scheduled" or len(days) != 3 or days[0] != validity.get("entry_session"):
        problem = "独立交易日历与唯一入场观察日未核验"
    else:
        try:
            opened = datetime.combine(date.fromisoformat(days[0]), time(9, 30), CN_TZ)
            if timestamp(report["cutoff"]) >= opened or timestamp(report["created_at"]) >= opened:
                problem = "报告未在入场观察日开盘前冻结"
        except (ValueError, TypeError, KeyError):
            problem = "缺少冻结报告时点"
    rows = []
    regime = str((report.get("ecology") or {}).get("state") or (report.get("market_state") or {}).get("phase") or "unknown")
    for candidate in report.get("candidates", [])[:5]:
        row = {"symbol": candidate["symbol"], "name": candidate["name"], "mode": candidate["mode"],
               "sector": candidate["sector"], "regime": regime, "reason": problem, "labels": []}
        issue = problem or ("冻结条件版本不受支持，不能跳过未知条件" if not _condition_schema(candidate) else "")
        entry, state, reason = None, "unavailable", issue
        session_data = (evidence.get("sessions") or {}).get(candidate["symbol"], {})
        pairs = [(date.fromisoformat(day), session_data.get(day, {})) for day in days]
        if not issue and now < datetime.combine(pairs[0][0], time(9, 31), CN_TZ):
            state, reason = "pending", "入场观察日尚未开始或首根分钟未完成"
        elif not issue:
            entry, state, reason = _entry(candidate, pairs[0][1], pairs[0][0], now, policy)
        row["reason"] = reason
        if entry:
            row.update(signal_time=_iso(entry["signal"]), entry_time=_iso(entry["time"]),
                       entry_price=entry["price"], quantity=entry["quantity"])
        for horizon in range(1, 4):
            day = days[horizon - 1] if len(days) >= horizon else None
            if entry:
                label = _exit_label(candidate, entry, pairs, horizon, now, policy)
            else:
                future = day and now < datetime.combine(date.fromisoformat(day), time(15), CN_TZ)
                label = _label(horizon, day, "pending" if future and state != "unavailable" else state, reason)
                if state == "no_entry" and not future:
                    label.update(mature=True, outcome_available_at=f"{day}T15:00:00+08:00")
            row["labels"].append(label)
        rows.append(row)
    states = [label["state"] for row in rows for label in row["labels"]]
    status = ("unavailable" if not states or all(state == "unavailable" for state in states) else
              "pending" if all(state == "pending" for state in states) else
              "complete" if all(state in {"no_entry", "exited", "not_executable"} for state in states) else "partial")
    # The digest uses only consumed, as-of-filtered adapter evidence. Callers
    # must not persist future rows as if they were available in this evaluation.
    fingerprint = hashlib.sha256(json.dumps({"report": report.get("input_version"), "policy": policy.model_dump(),
                                            "rows": rows, "evidence": evidence.get("fingerprint")},
                                           sort_keys=True, default=str, allow_nan=False).encode()).hexdigest()[:24]
    rule_cohort = (f"{report.get('rule_version', 'unknown')}/exec-{VERSION}/policy-{policy.version}"
                   f"/eco-{(report.get('ecology') or {}).get('version', 'legacy')}")
    return ExecutionEvaluation.model_validate({
        "report_id": report["id"], "evaluated_at": _iso(now), "report_created_at": report.get("created_at"), "input_version": fingerprint,
        "rule_version": rule_cohort, "status": status, "rows": rows,
        "costs": policy.costs.model_dump(), "evidence_summary": evidence.get("summary", []),
        "limitations": [*LIMITATIONS, *evidence.get("limitations", [])],
    }).model_dump(mode="json")


def evaluate_executions(repo, report: dict, snapshot: dict, *, now: datetime | None = None,
                        evidence: dict | None = None) -> dict:
    """Bounded repository orchestration; an explicit evidence override is for tests."""
    from app.services.market_game_execution_service import build_execution_evidence

    now = timestamp(now or cn_now())
    evidence = evidence if evidence is not None else build_execution_evidence(repo, report, snapshot, now=now)
    return simulate_executions(report, evidence, now=now)
