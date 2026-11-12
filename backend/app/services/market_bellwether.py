"""盘中风向标观察 —— 盘前选定、盘中只跟踪。

风向标只用上一交易日(T-1)已收盘的数据选出: 空间最高的连板股(情绪)、
涨停主线的龙头与成交额最大的中军。盘中不再重选, 只把这份名单对照当前行情
归类状态, 连同昨日涨停/炸板股的今日表现给出"进攻 / 分歧 / 退潮"的规则结论。
今天才冒头的强势股不会顶替名单, 否则龙头会随涨幅漂移, 失去参照意义。

只读已标准化的行情与落盘数据, 判定完全由程序完成, 不调用 AI、不推送。
与复盘页已有的"盘前风向标"(同花顺竞价名单, auction_benchmark)是两回事。

口径: change_pct、avg_pct、up_ratio、seal_rate 均为小数制; 封单量单位为手。
状态与结论阈值是工程实验门槛, 不是经过回测的参数。
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

import polars as pl

from app.market_time import CN_TZ, cn_now, in_continuous_session
from app.services.first_board_context import (
    live_ladder,
    load_mainline_members,
    load_mainlines,
    previous_boards,
)
from app.services.market_mainline import MEMBERSHIP_NOTE, load_risk_warning_symbols
from app.services.market_recap_intraday import _sessions

logger = logging.getLogger(__name__)

TOP_MAINLINES = 3            # 跟踪的主线数
MIN_EMOTION_BOARDS = 2       # 首板不构成空间高度
STRONG_CHANGE = 0.03         # 涨跌幅达到 ±3% 归为强势/走弱
FEEDBACK_STRONG = (0.02, 0.6)   # 昨日涨停股: 均涨 ≥2% 且红盘率 ≥60% 为赚钱效应
FEEDBACK_WEAK_RATIO = 0.4       # 均涨为负或红盘率 <40% 为亏钱效应
STALE_SECONDS = 120          # 连续竞价时段行情超过此秒数未更新即不下结论
_CACHE_TTL = 5.0

ROLE_LABELS = {"emotion": "情绪", "leader": "龙头", "core": "中军", "feedback": "昨日涨停反馈"}
STATE_LABELS = {"sealed": "封板", "broken": "炸板", "strong": "强势", "flat": "震荡",
                "weak": "走弱", "limit_down": "跌停", "unknown": "待确认"}
VERDICT_LABELS = {"attack": "进攻", "divergence": "分歧", "retreat": "退潮", "unknown": "待确认"}
_STATE_SIGNAL = {"sealed": "strong", "strong": "strong", "flat": "neutral", "broken": "weak",
                 "weak": "weak", "limit_down": "weak", "unknown": "unknown"}
_PREV_COLUMNS = ["symbol", "date", "consecutive_limit_ups", "amount", "signal_limit_up",
                 "signal_broken_limit_up"]


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _symbol(value: object) -> str:
    return str(value or "").strip().upper()


# ───────────────────────── 盘前选股(只看 T-1) ─────────────────────────

def tracked_mainlines(mainlines: dict) -> list[dict]:
    """实际跟踪的主线: 概念优先, 没有概念主线时才用行业, 两者不混排。"""
    items = [item for item in mainlines.get("items") or [] if isinstance(item.get("name"), str)]
    kind = "concept" if any(item.get("kind") == "concept" for item in items) else "industry"
    return sorted((item for item in items if item.get("kind") == kind),
                  key=lambda item: item.get("rank") or 0)[:TOP_MAINLINES]


def select_bellwethers(prev: pl.DataFrame, mainlines: dict, members: dict[str, list[dict]], *,
                       excluded: frozenset[str] = frozenset()) -> list[dict]:
    """T-1 收盘行情 + T-1 主线 → 风向标名单, 同一只股票的多个角色合并为一行。

    prev 需含 symbol/consecutive_limit_ups/amount; members 为
    load_mainline_members 的 {股票: [命中的主线]}。
    """
    if prev.is_empty() or not {"symbol", "consecutive_limit_ups", "amount"}.issubset(prev.columns):
        return []
    boards: dict[str, int] = {}
    amounts: dict[str, float] = {}
    for symbol, consecutive, amount in prev.select("symbol", "consecutive_limit_ups", "amount").iter_rows():
        key = _symbol(symbol)
        if not key or key in excluded:
            continue
        boards[key] = int(_finite(consecutive) or 0)
        value = _finite(amount)
        if value is not None:
            amounts[key] = value

    picked: dict[str, list[dict]] = {}

    def add(symbol: str, role: str, detail: str) -> None:
        roles = picked.setdefault(symbol, [])
        if not any(item["key"] == role and item["detail"] == detail for item in roles):
            roles.append({"key": role, "label": ROLE_LABELS[role], "detail": detail})

    top = max(boards.values(), default=0)
    if top >= MIN_EMOTION_BOARDS:
        tied = sorted((symbol for symbol, count in boards.items() if count == top),
                      key=lambda symbol: (-amounts.get(symbol, 0.0), symbol))
        suffix = f"，同高度 {len(tied)} 只" if len(tied) > 1 else ""
        add(tied[0], "emotion", f"昨日 {top} 连板{suffix}")

    chosen = tracked_mainlines(mainlines)
    kind = chosen[0].get("kind") if chosen else None
    for item in chosen:
        leader = _symbol(item.get("leader_symbol"))
        if leader and leader not in excluded:
            add(leader, "leader", f"{item['name']} 龙头")
    for item in chosen:
        pool = [symbol for symbol, hits in members.items() if symbol in amounts and any(
            hit.get("kind") == kind and hit.get("name") == item["name"] for hit in hits)]
        if pool:
            add(min(pool, key=lambda symbol: (-amounts[symbol], symbol)), "core", f"{item['name']} 中军")

    return [{"symbol": symbol, "roles": roles, "boards_prev": boards.get(symbol)}
            for symbol, roles in picked.items()]


# ───────────────────────── 盘中状态与结论 ─────────────────────────

def stock_state(row: dict | None, sealed: dict | None = None) -> str:
    """当前行情行 → 状态键(见 STATE_LABELS)。sealed 为五档封板判定, 假涨停按炸板。"""
    if not row:
        return "unknown"
    if row.get("signal_limit_down") is True:
        return "limit_down"
    if row.get("signal_limit_up") is True or (_finite(row.get("consecutive_limit_ups")) or 0) > 0:
        return "broken" if sealed is not None and sealed.get("sealed") is False else "sealed"
    if row.get("signal_broken_limit_up") is True:
        return "broken"
    change = _finite(row.get("change_pct"))
    if change is None:
        return "unknown"
    if change >= STRONG_CHANGE:
        return "strong"
    return "weak" if change <= -STRONG_CHANGE else "flat"


def pool_performance(today: pl.DataFrame, pool: Iterable[str]) -> dict | None:
    """一组股票今日的平均涨幅与红盘率(小数制); 无有效行情返回 None。"""
    if today.is_empty() or not {"symbol", "change_pct"}.issubset(today.columns):
        return None
    pct = today.filter(pl.col("symbol").is_in(list(pool)))["change_pct"].drop_nulls()
    if pct.len() == 0:
        return None
    return {"count": int(pct.len()), "avg_pct": float(pct.mean()),
            "up_ratio": float((pct > 0).sum()) / pct.len()}


def role_signal(items: list[dict], role: str) -> str:
    """某一角色的合并信号: 半数走弱即弱, 半数走强才强, 全部未知为未知。"""
    signals = [item["signal"] for item in items
               if any(entry["key"] == role for entry in item["roles"]) and item["signal"] != "unknown"]
    if not signals:
        return "unknown"
    if signals.count("weak") * 2 >= len(signals):
        return "weak"
    return "strong" if signals.count("strong") * 2 >= len(signals) else "neutral"


def feedback_signal(perf: dict | None) -> str:
    if not perf:
        return "unknown"
    if perf["avg_pct"] < 0 or perf["up_ratio"] < FEEDBACK_WEAK_RATIO:
        return "weak"
    strong = perf["avg_pct"] >= FEEDBACK_STRONG[0] and perf["up_ratio"] >= FEEDBACK_STRONG[1]
    return "strong" if strong else "neutral"


def verdict(signals: dict[str, str]) -> dict:
    """各角色信号 → 综合结论, 按最弱的信号定性; 可判定角色不足两类时不下结论。"""
    known = {role: signal for role, signal in signals.items() if signal != "unknown"}
    weak = [ROLE_LABELS[role] for role, signal in known.items() if signal == "weak"]
    strong = [ROLE_LABELS[role] for role, signal in known.items() if signal == "strong"]
    if len(known) < 2:
        key, reason = "unknown", "可判定的风向标不足两类"
    elif len(weak) >= 2:
        key, reason = "retreat", f"{'、'.join(weak)}同时走弱"
    elif not weak and len(strong) >= 2:
        key, reason = "attack", f"{'、'.join(strong)}走强，没有哪一类走弱"
    elif weak:
        key, reason = "divergence", f"{'、'.join(weak)}走弱" + (f"，{'、'.join(strong)}仍强" if strong else "")
    else:
        key, reason = "divergence", "没有哪一类风向标走弱，但走强的不足两类"
    return {"key": key, "label": VERDICT_LABELS[key], "reason": reason, "signals": signals}


# ───────────────────────── 装配 ─────────────────────────

def _excluded_symbols(data_dir: Path) -> frozenset[str]:
    """与主线计算同一开关: 默认剔除风险警示股。"""
    try:
        from app.services import preferences

        if not preferences.get_sentiment_exclude_st():
            return frozenset()
    except Exception:  # noqa: BLE001 - 偏好不可读时沿用主线的默认剔除
        logger.debug("bellwether: exclude-st preference unavailable", exc_info=True)
    return load_risk_warning_symbols(data_dir)


def _unavailable(reason: str, now: datetime, as_of: date | None = None) -> dict:
    return {"status": "unavailable", "reason": reason, "as_of": str(as_of) if as_of else None,
            "reference_date": None, "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "quote_time": None, "live": False, "stale": False,
            "verdict": verdict({}), "items": [], "feedback": {"limit_up": None, "broken": None},
            "ladder": None, "mainlines": [], "notes": []}


def _sealed_map(depth_service, as_of: date) -> dict | None:
    """五档封板判定; 无能力、未就绪或读取失败时返回 None(不修正, 不估算)。"""
    if depth_service is None:
        return None
    try:
        if not depth_service.is_sealed_ready(as_of):
            return None
        return depth_service.get_sealed_map(as_of, is_down=False) or None
    except Exception:  # noqa: BLE001 - 封单是可选增强, 失败不影响风向标
        logger.warning("bellwether: sealed map unavailable", exc_info=True)
        return None


def _flags(prev: pl.DataFrame, column: str) -> list[str] | None:
    if column not in prev.columns:
        return None
    return prev.filter(pl.col(column).fill_null(False))["symbol"].to_list()


def build_snapshot(repo, *, quote_service=None, depth_service=None, now: datetime | None = None) -> dict:
    """装配一次风向标快照; 任何关键输入缺失都返回 status=unavailable, 不用别的日期顶替。"""
    now = now or cn_now()
    current, as_of = (quote_service.get_enriched_today() if quote_service is not None
                      else repo.get_enriched_latest())
    if type(as_of) is not date or current is None or current.is_empty() or "symbol" not in current.columns:
        return _unavailable("暂无行情数据", now)
    sessions = _sessions(as_of)
    reference = max((day for day in sessions or [] if day < as_of), default=None)
    if not sessions or as_of not in sessions or reference is None:
        return _unavailable("交易日历不可用, 无法确定上一交易日", now, as_of)
    prev = repo.get_enriched_range(reference, reference, columns=_PREV_COLUMNS)
    if prev is None or prev.is_empty():
        return _unavailable(f"缺少上一交易日({reference})的收盘数据, 暂无法选出风向标", now, as_of)

    data_dir = repo.store.data_dir
    notes = []
    try:
        mainlines = load_mainlines(data_dir, as_of=as_of, reference_date=reference, top_n=TOP_MAINLINES)
        members = load_mainline_members(repo, mainlines) if mainlines.get("status") == "ready" else {}
    except Exception:  # noqa: BLE001 - 主线缺失只少两类风向标, 情绪与反馈仍可用
        logger.warning("bellwether: mainline load failed", exc_info=True)
        mainlines, members = {"status": "unavailable", "reason": "主线读取失败", "items": []}, {}
    if mainlines.get("status") == "ready":
        notes.append(MEMBERSHIP_NOTE)
    else:
        notes.append(f"主线龙头与中军暂缺: {mainlines.get('reason') or '主线数据不可用'}")
    selected = select_bellwethers(prev, mainlines, members, excluded=_excluded_symbols(data_dir))

    symbols = [item["symbol"] for item in selected]
    rows: dict[str, dict] = {}
    duplicated: set[str] = set()
    for row in current.filter(pl.col("symbol").is_in(symbols)).iter_rows(named=True):
        key = _symbol(row["symbol"])
        if key in rows:
            # 重复报价无法确定哪条有效, 整只按待确认处理
            duplicated.add(key)
        rows[key] = row
    for key in duplicated:
        rows.pop(key)
    names = {key: row.get("name") for key, row in rows.items() if row.get("name")}
    missing = [symbol for symbol in symbols if symbol not in names]
    if missing:
        try:
            names.update(repo.get_name_map(missing))
        except Exception:  # noqa: BLE001 - 名称缺失时退回代码
            logger.debug("bellwether: name map unavailable", exc_info=True)

    sealed = _sealed_map(depth_service, as_of)
    items = []
    for entry in selected:
        symbol = entry["symbol"]
        row = rows.get(symbol)
        state = stock_state(row, (sealed or {}).get(symbol))
        seal = (sealed or {}).get(symbol) or {}
        items.append({
            **entry, "name": names.get(symbol) or symbol,
            "boards_today": int(_finite(row.get("consecutive_limit_ups")) or 0) if row else None,
            "change_pct": _finite(row.get("change_pct")) if row else None,
            "state": state, "signal": _STATE_SIGNAL[state],
            "seal_vol": int(seal["vol"]) if state == "sealed" and _finite(seal.get("vol")) is not None else None,
        })

    limit_pool, broken_pool = _flags(prev, "signal_limit_up"), _flags(prev, "signal_broken_limit_up")
    feedback = {
        "limit_up": pool_performance(current, limit_pool) if limit_pool else None,
        "broken": pool_performance(current, broken_pool) if broken_pool else None,
    }
    fake_up = sum(1 for value in sealed.values() if value.get("sealed") is False) if sealed else None
    ladder = live_ladder(current, previous_boards(prev, reference), fake_up=fake_up)

    live = as_of == now.astimezone(CN_TZ).date() and in_continuous_session(now)
    stamp = _finite(current["quote_ts"].max()) if "quote_ts" in current.columns else None
    quote_time = datetime.fromtimestamp(stamp / 1000, CN_TZ) if stamp else None
    stale = live and (quote_time is None or (now - quote_time).total_seconds() > STALE_SECONDS)
    result = verdict({role: role_signal(items, role) for role in ("emotion", "leader", "core")}
                     | {"feedback": feedback_signal(feedback["limit_up"])})
    if stale:
        result = {**result, "key": "unknown", "label": VERDICT_LABELS["unknown"],
                  "reason": f"行情超过 {STALE_SECONDS // 60} 分钟未更新, 暂不下结论"}
    return {
        "status": "ready", "reason": "", "as_of": str(as_of), "reference_date": str(reference),
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "quote_time": quote_time.strftime("%Y-%m-%dT%H:%M:%S") if quote_time else None,
        "live": live, "stale": stale, "verdict": result, "items": items, "feedback": feedback,
        "ladder": {key: ladder[key] for key in ("limit_up", "broken", "limit_down", "max_consecutive",
                                                 "seal_rate", "promo_rate", "sealed_corrected")},
        "mainlines": [{key: item.get(key) for key in ("kind", "name", "rank")}
                      for item in tracked_mainlines(mainlines)],
        "notes": notes,
    }


_cache: tuple[float, dict] | None = None
_cache_lock = threading.Lock()


def invalidate_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def get_snapshot(repo, *, quote_service=None, depth_service=None) -> dict:
    """短 TTL 缓存的快照, 供接口轮询; 计算在锁外, 并发重算无副作用。"""
    global _cache
    with _cache_lock:
        if _cache is not None and time.monotonic() - _cache[0] < _CACHE_TTL:
            return _cache[1]
    snapshot = build_snapshot(repo, quote_service=quote_service, depth_service=depth_service)
    with _cache_lock:
        _cache = (time.monotonic(), snapshot)
    return snapshot
