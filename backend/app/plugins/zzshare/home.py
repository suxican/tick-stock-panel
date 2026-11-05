"""官网首页的题材榜单。候选分组、席位顺序和核心股均由上游确定。"""
from __future__ import annotations

from datetime import time

from app.plugins.zzshare import ZzshareError
from app.plugins.zzshare.research import _day, _number, _rows, _stock, _text, _timestamp


def _iso_day(value: str) -> str:
    if _day(value) != value:
        raise ValueError("日期必须使用 YYYY-MM-DD")
    return value


def _count(value: int) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("数量无效")
    return value


def _required_number(value: object) -> float:
    number = _number(value)
    if number is None:
        raise ValueError("缺少数值")
    return number


def _core(row: dict, day: str) -> dict:
    symbol = _stock(row["code"])["symbol"]
    if not symbol or _day(row["quote_date"]) != day:
        raise ValueError("核心股代码或行情日期无效")
    if row.get("quote_time") and _timestamp(row["quote_time"])[:10] != day:
        raise ValueError("核心股行情时间不属于该交易日")
    return {
        "symbol": symbol, "name": _text(row["name"]), "role": _text(row["role"]),
        "change_pct": _number(row.get("pct")), "price_yuan": _number(row.get("price")),
        "core_score": _number(row.get("core_score")),
    }


def _topic(row: dict, day: str) -> dict:
    tid = str(row["id"])
    if not tid.isascii() or not tid.isdigit() or int(tid) <= 0:
        raise ValueError("题材 ID 无效")
    scope = row["selection_scope"]
    if scope not in ("strategy", "all"):
        raise ValueError("题材候选范围无效")
    members = _count(row["total_count"])
    valid = _count(row["stock_count"])
    rising = _count(row["up_count"])
    strong = _count(row["leader_count"])
    limit_up = _count(row["limit_up_count"])
    limit_down = _count(row["limit_down_count"])
    rising_pct = _required_number(row["up_ratio"])
    score = _required_number(row["score"])
    if not strong <= rising <= valid <= members or limit_up + limit_down > valid:
        raise ValueError("题材成分计数不一致")
    if not 0 <= rising_pct <= 100 or not 0 <= score <= 100:
        raise ValueError("上涨占比或强度分越界")
    mainline = row.get("mainline")
    if mainline is not None and not isinstance(mainline, dict):
        raise ValueError("持续性标记无效")
    mainline = mainline or {}
    return {
        "tid": tid, "name": _text(row["name"]), "score": score, "scope": scope,
        "avg_change_pct": _required_number(row["today_pct"]), "rising_pct": rising_pct,
        "rising_count": rising, "valid_count": valid, "member_count": members, "strong_count": strong,
        "limit_up_count": limit_up, "limit_down_count": limit_down,
        "mainline_tag": _text(mainline["tag"]) if mainline.get("tag") else None,
        "continuity_score": _number(mainline.get("continuity_score")),
        "core_stocks": [_core(s, day) for s in _rows(row["core_stocks"])],
    }


def fetch_home_topics(get, requested: str | None) -> dict:
    params = {"date": _iso_day(requested)} if requested is not None else {}
    data = get("/v3/topic/home-topics", params)
    try:
        day = _iso_day(data["trade_date"])
        if requested is not None and day != requested:
            raise ValueError("交易日不符")
        rows = _rows(data["items"])
        if len(rows) > 3:
            raise ValueError("超过首页席位数")
        items = [_topic(r, day) for r in rows]
        if len({r["tid"] for r in items}) != len(items):
            raise ValueError("首页题材重复")
        calc = data["calculated_at"]
        # 历史回填的计算时间可能比交易日晚数周，必须保留真实日期。
        asof = _timestamp(calc, timespec="auto") if calc is not None else None
        if (items and not asof) or (asof and asof[:10] < day):
            raise ValueError("榜单计算时间缺失或早于交易日")
        clock = data.get("market_snapshot_at")
        if clock is not None and time.fromisoformat(clock).isoformat(timespec="minutes") != clock:
            raise ValueError("行情快照时刻无效")
        candidates = _count(data["candidate_count"])
        strategy = _count(data["strategy_candidate_count"])
        if strategy > candidates or len(items) > candidates or type(data["is_trading"]) is not bool:
            raise ValueError("候选数量或市场状态无效")
        return {
            "date": day, "source_asof": asof, "market_snapshot_at": clock,
            "source": _text(data["source"]), "cache_status": _text(data["cache_status"]),
            "is_trading": data["is_trading"], "selection_rule": _text(data["selection_rule"]),
            "candidate_count": candidates, "strategy_candidate_count": strategy, "items": items,
        }
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise ZzshareError("zzshare 官网题材榜日期或字段无效") from exc


def fetch_home_dates(get) -> list[str]:
    raw = get("/v3/topic/home-topics/history-dates", {"limit": 60})
    try:
        if not isinstance(raw, list):
            raise ValueError("日期目录格式无效")
        dates = [_iso_day(d) for d in raw]
        if len(set(dates)) != len(dates):
            raise ValueError("日期目录含重复交易日")
        return sorted(dates, reverse=True)
    except (ValueError, TypeError) as exc:
        raise ZzshareError("zzshare 官网历史日期目录无效") from exc
