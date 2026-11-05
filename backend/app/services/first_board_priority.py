"""Experimental first-board priority over evidence available at the observation.

Points order candidates within their trading state. This pure function performs
no IO, grants no permission, and does not estimate fills or profit probability.
"""
from __future__ import annotations

import math

from app.strategy.first_board import FirstBoardRules, meets_min_change

RULE_VERSION = "priority-v1"


def _finite(value: object) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def _part(maximum: int, points: float | None, reason: str, *, known: int | None = None) -> dict:
    return {"points": round(points, 1) if points is not None else None, "max_points": maximum,
            "known_max": (maximum if known is None else known) if points is not None else 0, "reason": reason}


def _stock(row: dict, rules: FirstBoardRules) -> dict:
    facts = row.get("evidence") or {}
    price, breakout = row.get("price"), facts.get("breakout_price_raw")
    distance, amount, turnover = row.get("distance_to_limit_pct"), facts.get("amount"), row.get("turnover_rate")
    if (not all(_finite(value) for value in (price, breakout, distance, amount, turnover))
            or min(price, breakout) <= 0 or min(distance, amount, turnover) < 0):
        return _part(40, None, "缺少有效原价、突破基准、距涨停或量额")
    proximity = (max(0., 1. - distance / (2 * rules.approaching_distance))
                 if rules.approaching_distance > 0 else float(distance == 0))
    breakout_points = 10 * (price > breakout)
    amount_points = 5 * (amount >= rules.min_amount)
    turnover_points = 5 * (rules.min_turnover_rate <= turnover <= rules.max_turnover_rate)
    return _part(40, breakout_points + amount_points + turnover_points + 20 * proximity,
                 f"突破 {breakout_points}/10，成交额 {amount_points}/5，换手 {turnover_points}/5，"
                 f"距涨停 {distance:.2%}（接近度 {20 * proximity:.1f}/20）")


def _sector(groups: list[dict], radar: dict) -> tuple[dict, dict | None]:
    corroborated = any((radar.get(key) or {}).get("status") == "ready" for key in ("plate", "theme"))
    best = None
    for group in groups:
        rank, total = group.get("peer_rank"), group.get("peer_total")
        if rank is None or not total:
            continue
        breadth = group["peer_up_ratio"]
        mean = group["peer_mean_change_pct"]
        excess = mean - group["peer_market_mean_change_pct"]
        # Rank within this source and field; never award a first-place bonus
        # for a source with a single eligible sector and no comparator.
        rank_points = 12 * (total - rank) / (total - 1) if total > 1 else 0.
        relative_points = 10 * max(0., min(1., excess / .03))
        breadth_points = 6 * max(0., min(1., (breadth - .5) / .3))
        score = rank_points + relative_points + breadth_points + 2 * (group.get("peer_persistent") is True)
        # Deterministic tie breaks favor stronger breadth and better coverage.
        key = (-score, -breadth, -group["peer_coverage"], group["id"])
        if best is None or key < best[0]:
            best = key, group, score
    if best is None:
        if corroborated:
            return _part(30, 2, "雷达已确认题材核心股或供应商榜首板块成分；本地同行强度待确认", known=2), None
        return _part(30, None, "剔除本股后有效同行不足，或成分与排名待确认"), None
    _, group, score = best
    keys = ("id", "kind", "name", "source_id", "field", "peer_rank", "peer_total",
            "peer_mean_change_pct", "peer_market_mean_change_pct", "peer_up_ratio",
            "peer_coverage", "peer_valid_count", "peer_count", "peer_strong", "stock_group_rank",
            "peer_persistent", "peer_reference_at")
    primary = {key: group.get(key) for key in keys}
    persistence = group.get("peer_persistent")
    known = 28 if persistence is None else 30
    note = ("；3–10分钟前至今观测均强，持续性加2分" if persistence is True else
            "；持续性待下一时点确认" if persistence is None else "；当前未持续强势")
    if corroborated:
        # The adapter matched the candidate by symbol. Its board may belong to
        # another taxonomy; keep the source identity separate from this group.
        score = min(known, score + 2.)
        note += "；另有当时可见的雷达题材成分或核心股佐证，补分最多2"
    return _part(30, score, f"剔除本股后 {group['name']} 在同来源分类排 "
                 f"{group['peer_rank']}/{group['peer_total']}，同行均涨 {group['peer_mean_change_pct']:.2%}，"
                 f"上涨占比 {group['peer_up_ratio']:.0%}{note}", known=known), primary


def _timing(attention: dict, group: dict | None) -> tuple[dict, str]:
    if not group:
        return _part(20, None, "缺少可核对的同板块角色"), "unknown"
    rising = attention.get("intraday_rising")
    leader = group.get("leader") or {}
    if group.get("follow") is True:
        role = "follower"
    elif group.get("stock_group_rank") == 1 and leader.get("at_limit") is not True:
        role = "leader"
    else:
        role = "ordinary"
    if rising is None:
        return _part(20, None, "分钟原价、完整均价或观察窗口待确认"), role
    if not rising:
        return _part(20, 0, "五分钟抬升、日内均价及回撤条件未同时通过"), role
    if role == "follower":
        return _part(20, 20, "同板块已观测领涨触板，随后出现完整五分钟抬升"), role
    if role == "leader":
        return _part(20, 20, "本股在已确认板块报价中涨幅并列或单独第一，分时已走强"), role
    if group.get("follow") is None:
        return _part(20, 10, "分时已走强，其他成员触板先后证据待确认", known=10), role
    return _part(20, 10, "分时已走强，尚无同方向领涨或跟随触板证据"), role


def _popularity(data: dict) -> dict:
    if data.get("status") == "unranked":
        return _part(10, 0, "当时人气榜有效，本股未进入该榜可见范围")
    rank = data.get("rank")
    if data.get("status") != "ready" or type(rank) is not int or not 1 <= rank <= 100:
        return _part(10, None, "缺少信号时已到达的个股人气榜")
    points = 8 if rank <= 5 else 7 if rank <= 20 else 5 if rank <= 50 else 3
    gain = data.get("rank_gain")
    if type(gain) is not int:
        return _part(10, points, f"人气第 {rank}，尚无可比的盘中上一期", known=8)
    bonus = min(2., max(0, gain) / 5)
    return _part(10, points + bonus, f"人气第 {rank}，较上一期名次变动 {gain:+d} 位")


def expire_priority(result: dict, reason: str) -> None:
    """Revoke live points when a previously fresh snapshot expires."""
    result.update(status="stale", score=None, known_max=0, parts={}, primary_sector=None,
                  role="unknown", can_buy=False, reason=reason, radar={})


def candidate_priority(row: dict, *, rules: FirstBoardRules, radar: dict | None, live: bool) -> dict:
    state = row.get("state")
    result = {"version": RULE_VERSION, "status": state or "invalid", "score": None, "known_max": 0,
              "parts": {}, "primary_sector": None, "role": "unknown", "can_buy": False,
              "reason": "", "radar": {}}
    if not live:
        expire_priority(result, "当前没有有效盘中报价")
        return result
    if state not in {"watch", "approaching"}:
        result["reason"] = {"sealed": "已触及涨停价，仅观察封板",
                            "broken": "已开板，停止临板排序"}.get(state, "个股行情或资格无效")
        return result
    if state == "approaching" and not row.get("can_buy") and not row.get("sector_shadow_eligible"):
        result.update(status="blocked", reason="原有行情、环境、行业或风险门槛未通过")
        return result
    change = row.get("change_pct")
    if not _finite(change) or not meets_min_change(change, rules.min_change_pct):
        result["reason"] = "尚未达到最低涨幅"
        return result
    stock = _stock(row, rules)
    if stock["points"] is None:
        result.update(status="invalid", reason=stock["reason"])
        return result
    attention = row.get("attention_context") or {}
    groups = attention.get("groups") or []
    radar = radar or {}
    sector, primary = _sector(groups, radar)
    chosen = next((group for group in groups if primary and group["id"] == primary["id"]), None)
    timing, role = _timing(attention, chosen)
    parts = {"stock": stock, "sector": sector, "timing": timing,
             "popularity": _popularity(radar.get("popularity") or {})}
    points = sum(part["points"] for part in parts.values() if part["points"] is not None)
    known = sum(part["known_max"] for part in parts.values())
    status = "watch" if state == "watch" else "shadow" if not row.get("can_buy") else "ready" if known == 100 else "partial"
    reason = {"watch": "尚待临板确认，仅供盯盘", "shadow": "强板块影子观察，未通过原行业门槛",
              "ready": "通过原规则，按盘中证据排序", "partial": "通过原规则，部分优先级证据待确认"}[status]
    sources = {key: radar[key] for key in ("popularity", "theme", "plate")
               if isinstance(radar.get(key), dict) and radar[key].get("status") in {"ready", "unranked"}}
    if radar.get("plates"):
        sources["plates"] = radar["plates"]
    result.update(status=status, score=round(points, 1), known_max=known, parts=parts, primary_sector=primary,
                  role=role, can_buy=status in {"ready", "partial"}, reason=reason, radar=sources)
    return result
