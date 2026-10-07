"""Freeze calendar windows and explicit requirements alongside a research plan."""
# User-facing Chinese prose intentionally uses fullwidth punctuation.
# ruff: noqa: RUF001
from __future__ import annotations

from datetime import datetime

from app.market_time import CN_TZ


def plan_validity(snapshot: dict) -> dict:
    calendar = snapshot.get("calendar") or {}
    verified = calendar.get("verified") is True and calendar.get("future_verified") is True
    sessions = list(calendar.get("exit_sessions") or []) if verified else []
    entry = calendar.get("entry_session") if verified else None
    historical = snapshot.get("metadata_scope") == "historical_unverified"
    scheduled = bool(verified and len(sessions) == 3 and entry == sessions[0] and not historical)
    return {
        "calendar_verified": verified,
        "entry_session": entry if scheduled else None,
        "observation_sessions": sessions if scheduled else [],
        "expires_at": f"{sessions[-1]}T15:00:00+08:00" if scheduled else None,
        "status": "research_only" if historical else "scheduled" if scheduled else "unverified",
        "reason": (
            "仅入场观察日可以启用计划；未触发不顺延，第3个计划交易日到期。" if scheduled else
            "历史资料未具备交易资格与板块时点证据，仅用于研究。" if historical else
            "独立交易日历或未来3个交易日覆盖未核验，不能确定入场日及到期日。"
        ),
    }


def candidate_conditions(candidate: dict) -> list[dict]:
    """All entry requirements must hold; any cancellation invalidates the plan.

    These are definitions for review, not a live monitor. In particular, a daily
    high/low intersecting the price range cannot certify an entry or a fill.
    """
    def item(key, phase, scope, metric, operator, value, description,
             *, upper=None, window="quote", samples=1):
        return {
            "id": key, "phase": phase, "scope": scope, "metric": metric,
            "operator": operator, "value": value, "upper": upper,
            "window": window, "minimum_samples": samples, "description": description,
        }

    return [
        item("price_zone", "entry", "stock", "last_price", "between", candidate["trigger_low"],
             "入场观察日最新价格位于原始价触发区间内", upper=candidate["trigger_high"]),
        item("holding_vwap", "entry", "stock", "close_above_session_vwap", "==", 1,
             "连续3根已完成1分钟K线收盘不低于当日累计成交均价；分钟缺档时无法确认",
             window="1m", samples=3),
        item("market_gate", "entry", "market", "new_risk_allowed", "==", 1,
             "市场风险门复核允许新增风险；收盘报告不能代替入场时复核", window="session"),
        item("sector_breadth", "entry", "sector", "breadth_up", ">=", 0.6,
             "同口径板块有效成员上涨占比至少60%；成员或行情缺失时无法确认"),
        item("execution", "entry", "execution", "tradable", "==", 1,
             "确认交易资格、卖盘、价格标尺及资金可用，满足条件也不代表已成交", window="execution"),
        item("structure_break", "cancel", "stock", "last_price", "<", candidate["invalidation_price"],
             "最新价格跌破结构失效参考价，取消未触发计划"),
        item("opening_gap", "cancel", "stock", "session_open", ">", candidate["trigger_high"],
             "入场观察日开盘价超过触发上限即取消，随后回落也不重新启用", window="session"),
        item("risk_veto", "cancel", "market", "new_risk_allowed", "==", 0,
             "市场风险门否决新增风险时取消计划", window="session"),
        item("sector_weak", "cancel", "sector", "breadth_up", "<", 0.4,
             "同口径板块上涨占比低于40%，视为板块整体转弱"),
    ]


def plan_status(report: dict, now: datetime) -> str:
    """Current display state stays separate from the archived prediction."""
    validity = report.get("validity") or {}
    if validity.get("status") != "scheduled":
        return validity.get("status", "unverified")
    current = now.astimezone(CN_TZ)
    expiry = datetime.fromisoformat(validity["expires_at"]).astimezone(CN_TZ)
    entry = datetime.fromisoformat(f"{validity['entry_session']}T09:30:00+08:00")
    entry_end = entry.replace(hour=15, minute=0)
    if current > expiry:
        return "expired"
    if current < entry:
        return "pending"
    if current <= entry_end:
        return "entry_window"
    return "observation_only"
