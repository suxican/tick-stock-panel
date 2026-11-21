"""首板「今日温度」: 只作显示的出手强度档位, 不参与任何买入判断。

依据今日封板率与昨日首板今日均涨两个当日数字; 阈值来自 2018-09 至 2026-11 的回测,
见 docs/superpowers/specs/2026-11-20-first-board-temperature-design.md。数据不足时给「待确认」。
"""
from __future__ import annotations

import math
from datetime import datetime
from datetime import time as day_time

from app.market_time import CN_TZ
from app.services.first_board_feedback import feedback_problem

TEMPERATURE_VERSION = "temperature-v1"
MIN_TOUCHES = 10
_LABELS = {"attack": "进攻", "probe": "试错", "rest": "休息", "unknown": "待确认"}
_STATUS_REASONS = {"waiting": "等待有效行情", "stale": "行情已过期", "error": "首板数据暂不可用",
                   "disabled": "自动盯盘已暂停"}


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _tier(seal: float, mean: float) -> str:
    if seal >= .70 and mean >= .02:
        return "attack"
    if seal < .60 or (seal < .70 and mean < 0):
        return "rest"
    return "probe"


def first_board_temperature(ladder: dict | None, feedback: dict | None, *, status: str,
                            as_of: str | None, now: datetime) -> dict:
    ladder = ladder or {}
    summary = ((feedback or {}).get("groups") or {}).get("all") or {}
    up, broken = _number(ladder.get("limit_up")), _number(ladder.get("broken"))
    touches = int(up + broken) if up is not None and broken is not None else None
    seal = _number(ladder.get("seal_rate"))
    mean = _number(summary.get("mean_change_pct"))
    result = {"version": TEMPERATURE_VERSION, "tier": "unknown", "label": _LABELS["unknown"],
              "seal_rate": seal, "touch_count": touches, "feedback_mean": mean,
              "feedback_sample": summary.get("sample_count"), "stage": None, "as_of": as_of,
              "observed_at": ladder.get("observed_at"), "reason": ""}
    if status not in {"ready", "closed"}:
        result["reason"] = _STATUS_REASONS.get(status, "暂无可用行情")
        return result
    local = now.replace(tzinfo=CN_TZ) if now.tzinfo is None else now.astimezone(CN_TZ)
    if status == "ready":
        result["stage"] = "early" if local.time() < day_time(10) else "session"
    else:
        finished = (as_of is not None and as_of < str(local.date())) or local.time() >= day_time(15)
        result["stage"] = "close" if finished else "session"
    if status == "closed" and as_of == str(local.date()) and local.time() < day_time(9, 30):
        # 集合竞价阶段一字板多、挂单可撤, 封板率系统性偏高, 不给档位。
        result["reason"] = "集合竞价阶段，待开盘"
    elif as_of is None or ladder.get("as_of") != as_of:
        result["reason"] = "缺少当日涨停梯队"
    elif touches is not None and touches < MIN_TOUCHES:
        result["reason"] = f"触板仅 {touches} 家，封板率尚不稳定"
    elif seal is None:
        result["reason"] = "封板率暂不可用（缺少炸板数据）"
    elif touches is None:
        result["reason"] = "触板家数未知，封板率尚不稳定"
    else:
        problem = feedback_problem(feedback, as_of, require_live=status == "ready")
        if problem:
            result["reason"] = problem
        else:
            tier = _tier(seal, mean)
            result.update(tier=tier, label=_LABELS[tier],
                          reason=f"封板率 {seal:.0%}，昨日首板今日均涨 {mean:+.2%}")
    return result
