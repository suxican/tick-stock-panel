"""Availability of evidence on the visible first-board stock pool, never a trading input."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from math import isfinite

from app.services.first_board_radar import _fresh as radar_fresh


def radar_clocks(radar: dict) -> dict:
    """Keep just the provenance needed for read-time expiry, including unranked stocks."""
    output = {}
    if not isinstance(radar, dict):
        return output
    for symbol, evidence in radar.items():
        if not isinstance(evidence, dict):
            continue
        stamps = []
        popularity = evidence.get("popularity")
        if isinstance(popularity, dict) and popularity.get("status") in ("ready", "unranked"):
            stamps.append((popularity["status"], popularity.get("source_at"), popularity.get("fetched_at")))
        plates = evidence.get("plates")
        for source in [evidence.get("theme"), evidence.get("plate"), *(plates if isinstance(plates, list) else [])]:
            if (isinstance(source, dict) and source.get("status") == "ready"
                    and source.get("source") in ("official_core", "vendor_plate")):
                stamps.append((source["source"], source.get("market_at") or source.get("source_at"), source.get("fetched_at")))
        stamps = [stamp for stamp in stamps if all(isinstance(x, str) for x in stamp)]
        if stamps:
            output[symbol] = tuple(dict.fromkeys(stamps))
    return output


def _current_minute(intraday: dict, now: datetime) -> bool:
    try:
        end = datetime.fromisoformat(intraday["as_of"])
    except (KeyError, TypeError, ValueError):
        return False
    return end.utcoffset() is not None and end == now.replace(second=0, microsecond=0) - timedelta(minutes=1)


def _supported_sectors(context: dict) -> set[str]:
    return {g["kind"] for g in context.get("groups", []) if g.get("kind") in {"industry", "concept"}
            and type(g.get("peer_rank")) is int and type(g.get("peer_total")) is int
            and 1 <= g["peer_rank"] <= g["peer_total"]}


def _priority(rows: list[dict]) -> str | None:
    modes = set()
    for row in rows:
        score = row.get("priority_score") or {}
        number = score.get("score")
        if type(number) not in (int, float) or not isfinite(number):
            continue
        state = score.get("status")
        if state in {"ready", "partial"} and score.get("can_buy") is True and row.get("can_buy") is True:
            return "actionable"
        if state in {"shadow", "watch"}:
            modes.add(state)
    return "shadow" if "shadow" in modes else "watch" if "watch" in modes else None


def _technical(rows: list[dict], trade_date: str | None, validation: dict) -> tuple[set[str], str | None]:
    known, refs = set(), set()
    if validation.get("allowed") is False:
        return known, None
    for row in rows:
        evidence = row.get("evidence") or {}
        tech = evidence.get("technical_shadow") or {}
        ref = tech.get("reference_date")
        if (tech.get("version") != "ma-macd-shadow-v1" or tech.get("status") not in {"ready", "partial"}
                or not isinstance(ref, str) or not trade_date or ref != evidence.get("reference_date")
                or tech.get("pattern") != row.get("pattern")
                or (validation.get("reference_date") and validation["reference_date"] != ref)
                or not ref < trade_date):
            continue
        refs.add(ref)
        known.update(k for k in ("ma", "macd") if type(tech.get(k + "_support")) is bool)
    return (known, refs.pop()) if known and len(refs) == 1 else (set(), None)


def _metric(count: int, total: int, detail: str, *, active: bool = True) -> dict:
    if not active:
        return {"status": "inactive", "known": 0, "total": total, "detail": "当前未使用盘中证据"}
    state = "empty" if total == 0 else "ready" if count == total else "partial" if count else "missing"
    return {"status": state, "known": count, "total": total, "detail": detail}


def build_data_status(snapshot: dict, *, now: datetime, fresh: set[str], clocks: dict, minute: dict,
                      membership_reason: str | None, enabled: bool, realtime_enabled: bool | None,
                      realtime_reason: str | None, in_session: bool, trading_day: bool | None, max_age: int) -> dict:
    day = snapshot.get("as_of")
    phase = {"ready": "live", "disabled": "paused"}.get(snapshot["status"], snapshot["status"])
    active = phase == "live"
    by_stock = defaultdict(list)
    for row in snapshot["rows"]:
        by_stock[row["symbol"]].append(row)
    total = len(by_stock)
    counts, refs, minute_issues = Counter(), set(), Counter()
    validation = (snapshot.get("environment") or {}).get("history_validation") or {}
    for symbol, rows in by_stock.items():
        historical, ref = _technical(rows, day, validation)
        counts.update(historical)
        if ref:
            refs.add(ref)
        if not active or symbol not in fresh:
            continue
        price = rows[0].get("price")
        if type(price) in (float, int) and isfinite(price) and price > 0:
            counts["quotes"] += 1
        context = rows[0].get("attention_context") or {}
        intraday = context.get("intraday") or {}
        if context.get("status") in {"ready", "partial"}:
            counts.update(_supported_sectors(context))
        if (context.get("status") in {"ready", "partial"} and minute.get("minute_price_basis") == "raw"
                and type(context.get("intraday_rising")) is bool):
            if _current_minute(intraday, now):
                counts["minute"] += 1
            else:
                minute_issues["分时末端尚未更新至当前已完成分钟"] += 1
        elif issue := intraday.get("reason"):
            minute_issues[issue] += 1
        sources = {kind for kind, at, fetched in clocks.get(symbol, ()) if radar_fresh(at, fetched, now)}
        counts.update(sources)
        if sources & {"official_core", "vendor_plate"}:
            counts["theme"] += 1
        mode = _priority(rows)
        if mode:
            counts[mode] += 1
    note = "快照时按同行报价计算，剔除本股后至少5只有效同行、覆盖达80%；读取时复核候选报价"
    if membership_reason:
        note += "；" + membership_reason
    technical_note = "参考日 " + "、".join(sorted(refs)) if refs else "缺少可核验的前一交易日证据"
    minute_note = minute.get("minute_reason") or "需要已确认原价及连续完整的五分钟窗口"
    if minute_issues and not minute.get("minute_reason"):
        issue, count = minute_issues.most_common(1)[0]
        minute_note = f"{count} 只：{issue}"
    metrics = {
        "quotes": _metric(counts["quotes"], total, f"同日有效原价，报价在 {max_age} 秒内", active=active),
        "industry": _metric(counts["industry"], total, note, active=active),
        "concept": _metric(counts["concept"], total, note, active=active),
        "minute": _metric(counts["minute"], total, minute_note, active=active),
        "popularity": _metric(counts["ready"] + counts["unranked"], total,
                              f"上榜 {counts['ready']}，已确认榜外 {counts['unranked']}", active=active),
        "theme": _metric(counts["theme"], total,
                         f"官网核心 {counts['official_core']}，供应商板块成员 {counts['vendor_plate']}，同股可重合",
                         active=active),
        "ma": _metric(counts["ma"], total, technical_note),
        "macd": _metric(counts["macd"], total, technical_note),
        "priority": _metric(counts["actionable"] + counts["shadow"] + counts["watch"], total,
                            f"可提示 {counts['actionable']}，影子 {counts['shadow']}，观察 {counts['watch']}；"
                            "使用快照时评分，读取时复核候选报价", active=active),
    }
    if day and day != str(now.date()):
        for key in ("ma", "macd"):
            metrics[key].update(status="inactive", known=0,
                                detail=f"归档快照 {day}，{technical_note}，当时覆盖 {counts[key]}/{total}")
    return {"version": 1, "trade_date": day, "observed_at": snapshot.get("observed_at"),
            "checked_at": now.isoformat(), "phase": phase, "monitor_enabled": enabled,
            "realtime_enabled": realtime_enabled,
            "realtime_status": "blocked" if realtime_reason else "available" if realtime_enabled is True else "unknown",
            "realtime_reason": realtime_reason, "in_session": in_session, "trading_day": trading_day,
            "same_day": day == str(now.date()), "quote_max_age_seconds": max_age, "symbol_count": total,
            "metrics": metrics, "sampling": snapshot["attention_sampling"]}
