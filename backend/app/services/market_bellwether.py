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
from app.services.market_mainline import (
    MEMBERSHIP_NOTE,
    filter_mainline_members,
    load_risk_warning_symbols,
)
from app.services.market_recap_intraday import _sessions
from app.services.rps_rotation import _load_concept_map_df

logger = logging.getLogger(__name__)

TOP_MAINLINES = 3            # 跟踪的主线数
MIN_EMOTION_BOARDS = 2       # 首板不构成空间高度
STRONG_CHANGE = 0.03         # 涨跌幅达到 ±3% 归为强势/走弱
FEEDBACK_STRONG = (0.02, 0.6)   # 昨日涨停股: 均涨 ≥2% 且红盘率 ≥60% 为赚钱效应
FEEDBACK_WEAK_RATIO = 0.4       # 均涨为负或红盘率 <40% 为亏钱效应
STALE_SECONDS = 120          # 连续竞价时段行情超过此秒数未更新即不下结论
_CACHE_TTL = 5.0

ROLE_LABELS = {"emotion": "情绪", "leader": "龙头", "weak_leader": "弱龙头", "core": "中军",
               "feedback": "昨日涨停反馈", "breadth": "板块广度"}
STATE_LABELS = {"sealed": "封板", "broken": "炸板", "strong": "强势", "flat": "震荡",
                "weak": "走弱", "limit_down": "跌停", "unknown": "待确认"}
VERDICT_LABELS = {"attack": "进攻", "neutral": "震荡", "divergence": "分歧", "retreat": "退潮",
                  "unknown": "待确认"}
MAX_NEWCOMERS = 5
MAX_SECTOR_NEW = 3
_STATE_SIGNAL = {"sealed": "strong", "strong": "strong", "flat": "neutral", "broken": "weak",
                 "weak": "weak", "limit_down": "weak", "unknown": "unknown"}
_PREV_COLUMNS = ["symbol", "date", "consecutive_limit_ups", "amount", "change_pct", "signal_limit_up",
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

    prev 需含 symbol/consecutive_limit_ups/amount, 缺 change_pct 时不选中军; members 为
    load_mainline_members 的 {股票: [命中的主线]}。
    """
    if prev.is_empty() or not {"symbol", "consecutive_limit_ups", "amount"}.issubset(prev.columns):
        return []
    boards: dict[str, int] = {}
    amounts: dict[str, float] = {}
    risers: set[str] = set()
    change = pl.col("change_pct") if "change_pct" in prev.columns else pl.lit(None)
    for symbol, consecutive, amount, pct in prev.select(
            "symbol", "consecutive_limit_ups", "amount", change.alias("_change")).iter_rows():
        key = _symbol(symbol)
        if not key or key in excluded:
            continue
        boards[key] = int(_finite(consecutive) or 0)
        value = _finite(amount)
        if value is not None:
            amounts[key] = value
        if (_finite(pct) or 0.0) > 0:
            risers.add(key)

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
        # 中军须昨日红盘: 只按成交额会选到宽概念里与主线无关、当天下跌的大票
        pool = [symbol for symbol, hits in members.items() if symbol in amounts and symbol in risers and any(
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


def role_signals(items: list[dict], roles: tuple[str, ...]) -> dict[str, str]:
    """各角色信号; 一只股票只计入它在 roles 中排最前的角色。

    同一只股票兼任多个角色(如情绪龙头即主线龙头、板块龙头即中军)时, 若各算
    一次, 单只股票就能凑出"两类走强"。
    """
    claimed: set[str] = set()
    output = {}
    for role in roles:
        holders = [item for item in items if item["symbol"] not in claimed
                   and any(entry["key"] == role for entry in item["roles"])]
        claimed.update(item["symbol"] for item in holders)
        output[role] = role_signal(holders, role)
    return output


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
        # 没有走弱只是缺少合力, 与"已有一类走弱"的分歧在交易上不同
        key, reason = "neutral", "没有哪一类走弱，但走强的不足两类"
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
    return {"scope": "market", "status": "unavailable", "reason": reason, "as_of": str(as_of) if as_of else None,
            "reference_date": None, "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "quote_time": None, "live": False, "stale": False,
            "verdict": verdict({}), "items": [], "newcomers": [],
            "feedback": {"limit_up": None, "broken": None},
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


def _prepare(repo, quote_service, now: datetime) -> tuple[dict | None, tuple[str, date | None] | None]:
    """当前行情 + 严格的上一交易日收盘; 缺任一项返回 (None, (原因, 观察日))。"""
    current, as_of = (quote_service.get_enriched_today() if quote_service is not None
                      else repo.get_enriched_latest())
    if type(as_of) is not date or current is None or current.is_empty() or "symbol" not in current.columns:
        return None, ("暂无行情数据", None)
    sessions = _sessions(as_of)
    reference = max((day for day in sessions or [] if day < as_of), default=None)
    if not sessions or as_of not in sessions or reference is None:
        return None, ("交易日历不可用, 无法确定上一交易日", as_of)
    prev = repo.get_enriched_range(reference, reference, columns=_PREV_COLUMNS)
    if prev is None or prev.is_empty():
        return None, (f"缺少上一交易日({reference})的收盘数据, 暂无法选出风向标", as_of)
    live = as_of == now.astimezone(CN_TZ).date() and in_continuous_session(now)
    stamp = _finite(current["quote_ts"].max()) if "quote_ts" in current.columns else None
    quote_time = datetime.fromtimestamp(stamp / 1000, CN_TZ) if stamp else None
    stale = live and (quote_time is None or (now - quote_time).total_seconds() > STALE_SECONDS)
    return {"current": current, "as_of": as_of, "reference": reference, "prev": prev, "sessions": sessions,
            "live": live, "stale": stale, "quote_time": quote_time}, None


def _quote_rows(current: pl.DataFrame, symbols: list[str]) -> dict[str, dict]:
    """唯一报价行; 重复报价无法确定哪条有效, 整只按待确认处理。"""
    rows: dict[str, dict] = {}
    duplicated: set[str] = set()
    for row in current.filter(pl.col("symbol").is_in(symbols)).iter_rows(named=True):
        key = _symbol(row["symbol"])
        if key in rows:
            duplicated.add(key)
        rows[key] = row
    for key in duplicated:
        rows.pop(key)
    return rows


def _names(repo, rows: dict[str, dict], symbols: list[str]) -> dict[str, str]:
    names = {key: row.get("name") for key, row in rows.items() if row.get("name")}
    missing = [symbol for symbol in symbols if symbol not in names]
    if missing:
        try:
            names.update(repo.get_name_map(missing))
        except Exception:  # noqa: BLE001 - 名称缺失时退回代码
            logger.debug("bellwether: name map unavailable", exc_info=True)
    return names


def _track(entry: dict, rows: dict[str, dict], names: dict[str, str], sealed: dict | None) -> dict:
    """名单中的一只 → 当前状态(展示与信号共用)。"""
    symbol = entry["symbol"]
    row = rows.get(symbol)
    state = stock_state(row, (sealed or {}).get(symbol))
    seal = (sealed or {}).get(symbol) or {}
    return {
        **entry, "name": names.get(symbol) or symbol,
        "boards_today": int(_finite(row.get("consecutive_limit_ups")) or 0) if row else None,
        "change_pct": _finite(row.get("change_pct")) if row else None,
        "state": state, "signal": _STATE_SIGNAL[state],
        "seal_vol": int(seal["vol"]) if state == "sealed" and _finite(seal.get("vol")) is not None else None,
    }


def _stamp(now: datetime, ctx: dict) -> dict:
    quote_time = ctx["quote_time"]
    return {"as_of": str(ctx["as_of"]), "reference_date": str(ctx["reference"]),
            "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "quote_time": quote_time.strftime("%Y-%m-%dT%H:%M:%S") if quote_time else None,
            "live": ctx["live"], "stale": ctx["stale"]}


def _stale_verdict(result: dict) -> dict:
    return {**result, "key": "unknown", "label": VERDICT_LABELS["unknown"],
            "reason": f"行情超过 {STALE_SECONDS // 60} 分钟未更新, 暂不下结论"}


def _today_limit_ups(current: pl.DataFrame, candidates: set[str], sealed: dict | None) -> list[dict]:
    """候选中今天封板的股票(五档判定为假涨停的不算), 按连板数、成交额降序。"""
    if not candidates:
        return []
    rows = _quote_rows(current, sorted(candidates))
    output = []
    for symbol, row in rows.items():
        if stock_state(row, (sealed or {}).get(symbol)) != "sealed":
            continue
        output.append({"symbol": symbol, "name": row.get("name") or symbol,
                       "boards_today": int(_finite(row.get("consecutive_limit_ups")) or 0),
                       "change_pct": _finite(row.get("change_pct")),
                       "_amount": _finite(row.get("amount")) or 0.0})
    output.sort(key=lambda item: (-item["boards_today"], -item["_amount"], item["symbol"]))
    for item in output:
        item.pop("_amount")
    return output


def market_newcomers(current: pl.DataFrame, prev: pl.DataFrame, listed: set[str], *,
                     excluded: frozenset[str], sealed: dict | None) -> list[dict]:
    """今天连板高度追平或超过昨日最高板、且不在盘前名单里的股票(切换信号候选)。"""
    if prev.is_empty() or "consecutive_limit_ups" not in current.columns:
        return []
    boards = [int(_finite(value) or 0) for symbol, value in
              prev.select("symbol", "consecutive_limit_ups").iter_rows() if _symbol(symbol) not in excluded]
    height = max([*boards, MIN_EMOTION_BOARDS])
    high = current.filter(pl.col("consecutive_limit_ups").cast(pl.Int64, strict=False).fill_null(0) >= height)
    candidates = {_symbol(symbol) for symbol in high["symbol"].to_list()} - listed - set(excluded)
    return _today_limit_ups(current, candidates, sealed)[:MAX_NEWCOMERS]


def build_snapshot(repo, *, quote_service=None, depth_service=None, now: datetime | None = None) -> dict:
    """装配一次风向标快照; 任何关键输入缺失都返回 status=unavailable, 不用别的日期顶替。"""
    now = now or cn_now()
    ctx, failure = _prepare(repo, quote_service, now)
    if ctx is None:
        return _unavailable(failure[0], now, failure[1])
    current, as_of, reference, prev = ctx["current"], ctx["as_of"], ctx["reference"], ctx["prev"]

    data_dir = repo.store.data_dir
    excluded = _excluded_symbols(data_dir)
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
    selected = select_bellwethers(prev, mainlines, members, excluded=excluded)

    symbols = [item["symbol"] for item in selected]
    rows = _quote_rows(current, symbols)
    names = _names(repo, rows, symbols)
    sealed = _sealed_map(depth_service, as_of)
    items = [_track(entry, rows, names, sealed) for entry in selected]
    newcomers = market_newcomers(current, prev, set(symbols), excluded=excluded, sealed=sealed)

    limit_pool, broken_pool = _flags(prev, "signal_limit_up"), _flags(prev, "signal_broken_limit_up")
    feedback = {
        "limit_up": pool_performance(current, limit_pool) if limit_pool else None,
        "broken": pool_performance(current, broken_pool) if broken_pool else None,
    }
    fake_up = sum(1 for value in sealed.values() if value.get("sealed") is False) if sealed else None
    ladder = live_ladder(current, previous_boards(prev, reference), fake_up=fake_up)

    result = verdict(role_signals(items, ("emotion", "leader", "core"))
                     | {"feedback": feedback_signal(feedback["limit_up"])})
    if ctx["stale"]:
        result = _stale_verdict(result)
    return {
        "scope": "market", "status": "ready", "reason": "", **_stamp(now, ctx),
        "verdict": result, "items": items, "newcomers": newcomers, "feedback": feedback,
        "ladder": {key: ladder[key] for key in ("limit_up", "broken", "limit_down", "max_consecutive",
                                                 "seal_rate", "promo_rate", "sealed_corrected")},
        "mainlines": [{key: item.get(key) for key in ("kind", "name", "rank")}
                      for item in tracked_mainlines(mainlines)],
        "notes": notes,
    }


# ───────────────────────── 持仓 / 自选所属板块 ─────────────────────────

USER_SCOPES = ("holdings", "watchlist")
SECTORS_PER_STOCK = {"concept": 2, "industry": 1}
MAX_USER_SECTORS = 8
USER_MAINLINE_TOP = 10        # 板块排序参考的主线名次范围
BREADTH_STRONG = (0.01, 0.6)  # 板块均涨 ≥1% 且红盘率 ≥60% 为走强
BREADTH_WEAK_RATIO = 0.4
BREADTH_MIN_COVERAGE = 0.8    # 与首板同行确认同一覆盖率门槛
RELATIVE_BAND = 0.01          # 本股相对板块均涨 ±1% 以内算同步
ACTIVE_DAYS = 5               # 活跃成员: 近 5 个交易日(截至 T-1)涨停过
MIN_ACTIVE = 5                # 活跃成员不足时退回全体成员


def _filter_config() -> dict | None:
    """None 让主线过滤自行读取偏好(成员数上下限与黑名单), 与主线口径一致。"""
    return None


def sector_map(mapping: pl.DataFrame, kind: str, filter_cfg: dict | None) -> pl.DataFrame:
    """成分映射(_sym_up, kind) → (_sym_up, sector): 过滤宽基/黑名单, 行业取前两级。"""
    if mapping.is_empty() or not {"_sym_up", kind}.issubset(mapping.columns):
        return pl.DataFrame(schema={"_sym_up": pl.Utf8, "sector": pl.Utf8})
    counts = mapping.group_by(kind).len().rename({"len": "_members"})
    allowed = filter_mainline_members(counts, kind, filter_cfg).select(kind)
    sector = pl.col(kind).cast(pl.Utf8)
    if kind == "industry":
        sector = sector.str.split("-").list.head(2).list.join("-")
    return (mapping.join(allowed, on=kind, how="semi")
            .select(pl.col("_sym_up").cast(pl.Utf8).str.to_uppercase(), sector.alias("sector")).unique())


def _prev_info(prev: pl.DataFrame) -> dict[str, tuple[int, float | None, float | None]]:
    """T-1 每只股票的 (连板数, 成交额, 涨跌幅); 重复行整只剔除。"""
    change = pl.col("change_pct") if "change_pct" in prev.columns else pl.lit(None)
    frame = prev.select(
        pl.col("symbol").cast(pl.Utf8).str.strip_chars().str.to_uppercase().alias("symbol"),
        pl.col("consecutive_limit_ups").cast(pl.Int64, strict=False).fill_null(0),
        pl.col("amount").cast(pl.Float64, strict=False), change.cast(pl.Float64, strict=False),
    ).unique("symbol", keep="none")
    return {symbol: (int(boards), _finite(amount), _finite(change))
            for symbol, boards, amount, change in frame.iter_rows()}


def _sector_watch(members: list[str], info: dict) -> list[dict]:
    """板块内的龙头与中军(只看 T-1); 没有涨停股时只给弱龙头, 不参与结论。"""
    picked: dict[str, list[dict]] = {}
    limit = [symbol for symbol in members if info[symbol][0] >= 1]
    if limit:
        leader = min(limit, key=lambda s: (-info[s][0], -(info[s][1] or 0.0), s))
        picked[leader] = [{"key": "leader", "label": ROLE_LABELS["leader"], "detail": f"昨日 {info[leader][0]} 板"}]
    else:
        rising = [symbol for symbol in members if info[symbol][2] is not None]
        if rising:
            leader = min(rising, key=lambda s: (-info[s][2], s))
            picked[leader] = [{"key": "weak_leader", "label": ROLE_LABELS["weak_leader"],
                               "detail": "昨日无涨停, 取涨幅最大"}]
    sized = [symbol for symbol in members if info[symbol][1] is not None and (info[symbol][2] or 0.0) > 0]
    if sized:
        core = min(sized, key=lambda s: (-info[s][1], s))
        picked.setdefault(core, []).append({"key": "core", "label": ROLE_LABELS["core"],
                                            "detail": "昨日红盘中成交额最大"})
    return [{"symbol": symbol, "roles": roles, "boards_prev": info[symbol][0]} for symbol, roles in picked.items()]


def pick_user_sectors(user: list[str], prev: pl.DataFrame, maps: dict[str, pl.DataFrame],
                      ranks: dict[tuple[str, str], int], *,
                      excluded: frozenset[str] = frozenset()) -> tuple[list[dict], list[str]]:
    """用户股票 → 要跟踪的板块(全部只用 T-1), 返回 (板块, 未覆盖的股票)。

    每只股票按「T-1 主线名次 → T-1 板块涨停家数 → T-1 板块均涨」挑概念与行业各
    SECTORS_PER_STOCK 个, 合并后整体按同一顺序截到 MAX_USER_SECTORS。风险警示股
    不当龙头/中军也不计入板块统计, 但用户自己持有的仍能找到所属板块。
    """
    wanted = list(dict.fromkeys(_symbol(symbol) for symbol in user if _symbol(symbol)))
    info = _prev_info(prev) if not prev.is_empty() else {}
    stats: dict[tuple[str, str], dict] = {}
    per_stock: dict[str, list[tuple[str, str]]] = {symbol: [] for symbol in wanted}
    for kind, mapping in maps.items():
        traded = mapping.filter(pl.col("_sym_up").is_in(list(info)))
        hits = traded.filter(pl.col("_sym_up").is_in(wanted))
        if hits.is_empty():
            continue
        for symbol, name in hits.select("_sym_up", "sector").iter_rows():
            per_stock[symbol].append((kind, name))
        names = set(hits["sector"].to_list())
        groups: dict[str, list[str]] = {}
        for symbol, name in traded.filter(pl.col("sector").is_in(list(names))).select("_sym_up", "sector").iter_rows():
            groups.setdefault(name, []).append(symbol)
        for name, all_members in groups.items():
            members = sorted(symbol for symbol in set(all_members) if symbol not in excluded)
            changes = [info[symbol][2] for symbol in members if info[symbol][2] is not None]
            stats[(kind, name)] = {
                "members": members, "all": set(all_members),
                "limit_up": sum(info[symbol][0] >= 1 for symbol in members),
                "avg": sum(changes) / len(changes) if changes else None,
            }

    def order(key: tuple[str, str]) -> tuple:
        item = stats[key]
        return (ranks.get(key, 999), -item["limit_up"], -(item["avg"] if item["avg"] is not None else -1.0),
                key[0], key[1])

    chosen: set[tuple[str, str]] = set()
    for symbol in wanted:
        for kind, limit in SECTORS_PER_STOCK.items():
            chosen.update(sorted((key for key in per_stock[symbol] if key[0] == kind), key=order)[:limit])
    tracked = sorted(chosen, key=order)[:MAX_USER_SECTORS]

    sectors = []
    for key in tracked:
        item = stats[key]
        sectors.append({
            "kind": key[0], "name": key[1], "mainline_rank": ranks.get(key),
            "member_count": len(item["members"]), "members": item["members"],
            "limit_up_prev": item["limit_up"], "avg_pct_prev": item["avg"],
            "mine": [symbol for symbol in wanted if symbol in item["all"]],
            "items": _sector_watch(item["members"], info),
        })
    covered = {symbol for sector in sectors for symbol in sector["mine"]}
    return sectors, [symbol for symbol in wanted if symbol not in covered]


def breadth_signal(perf: dict | None) -> str:
    if not perf or perf["coverage"] < BREADTH_MIN_COVERAGE:
        return "unknown"
    if perf["avg_pct"] < 0 or perf["up_ratio"] < BREADTH_WEAK_RATIO:
        return "weak"
    strong = perf["avg_pct"] >= BREADTH_STRONG[0] and perf["up_ratio"] >= BREADTH_STRONG[1]
    return "strong" if strong else "neutral"


def relative_strength(change: float | None, avg: float | None) -> str:
    if change is None or avg is None:
        return "unknown"
    if change - avg >= RELATIVE_BAND:
        return "stronger"
    return "weaker" if change - avg <= -RELATIVE_BAND else "inline"


def _recent_limit_ups(repo, sessions: list[date], reference: date) -> set[str] | None:
    """近 ACTIVE_DAYS 个交易日(截至 T-1)涨停过的股票; 缓存不覆盖时返回 None。"""
    window = sorted(day for day in sessions if day <= reference)[-ACTIVE_DAYS:]
    if not window:
        return None
    try:
        history = repo.get_enriched_range(window[0], reference, columns=["symbol", "date", "consecutive_limit_ups"])
    except Exception:  # noqa: BLE001 - 取不到历史时退回全体成员
        logger.warning("bellwether: recent limit-up history unavailable", exc_info=True)
        return None
    if history is None or history.is_empty() or "consecutive_limit_ups" not in history.columns:
        return None
    hit = history.filter(pl.col("consecutive_limit_ups").cast(pl.Int64, strict=False).fill_null(0) >= 1)
    return {_symbol(symbol) for symbol in hit["symbol"].to_list()}


def sector_breadth(current: pl.DataFrame, members: list[str], active: set[str] | None) -> dict | None:
    """板块今日广度。活跃成员(近期涨停过)足够时只算它们, 否则退回全体成员。

    宽概念常有两三百只成分, 全体均涨会冲淡真正参与题材的那部分资金的信号。
    """
    core = [symbol for symbol in members if active and symbol in active]
    if len(core) >= MIN_ACTIVE:
        result = _breadth(current, core)
        return {**result, "basis": "active", "size": len(core)} if result else None
    result = _breadth(current, members)
    return {**result, "basis": "all", "size": len(members)} if result else None


def _breadth(current: pl.DataFrame, members: list[str]) -> dict | None:
    perf = pool_performance(current, members)
    if perf is None or not members:
        return None
    today = current.filter(pl.col("symbol").is_in(members))
    limit = pl.lit(False)
    if "signal_limit_up" in today.columns:
        limit = limit | pl.col("signal_limit_up").cast(pl.Boolean, strict=False).fill_null(False)
    if "consecutive_limit_ups" in today.columns:
        limit = limit | (pl.col("consecutive_limit_ups").cast(pl.Int64, strict=False).fill_null(0) > 0)
    return {**perf, "coverage": perf["count"] / len(members), "limit_up": int(today.select(limit.sum()).item())}


def load_user_symbols(data_dir: Path, scope: str, group_id: str | None = None) -> tuple[list[str], str | None]:
    """持仓(批次提醒)或自选的股票代码; 只读代码, 不读数量与成本。"""
    if scope == "holdings":
        folder = Path(data_dir) / "user_data" / "lots"
        # 读取不应顺带创建目录(批次存储读时会建目录)
        if not folder.is_dir() or not any(folder.glob("lot_*.json")):
            return [], "还没有批次提醒持仓"
        from app.strategy import lots

        symbols = [lot.get("symbol") for lot in lots.load_all(Path(data_dir))]
        empty = "还没有批次提醒持仓"
    else:
        from app.services import watchlist

        rows = watchlist.list_symbols()
        if group_id:
            if group_id not in {group["id"] for group in watchlist.list_groups()}:
                return [], "自选分组不存在"
            rows = [row for row in rows if group_id in (row.get("group_ids") or [])]
        symbols = [row.get("symbol") for row in rows]
        empty = "该自选分组没有股票" if group_id else "自选为空"
    unique = list(dict.fromkeys(_symbol(symbol) for symbol in symbols if _symbol(symbol)))
    return (unique, None) if unique else ([], empty)


def _user_unavailable(scope: str, reason: str, now: datetime, as_of: date | None = None) -> dict:
    return {"scope": scope, "status": "unavailable", "reason": reason,
            "as_of": str(as_of) if as_of else None, "reference_date": None,
            "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S"), "quote_time": None,
            "live": False, "stale": False, "total": 0, "sectors": [], "uncovered": [], "notes": []}


def build_user_snapshot(repo, scope: str, symbols: list[str], *, quote_service=None, depth_service=None,
                        now: datetime | None = None) -> dict:
    """持仓/自选所属板块的风向标: 每个板块独立的龙头、中军与板块广度结论。"""
    now = now or cn_now()
    ctx, failure = _prepare(repo, quote_service, now)
    if ctx is None:
        return _user_unavailable(scope, failure[0], now, failure[1])
    current, as_of, reference = ctx["current"], ctx["as_of"], ctx["reference"]
    data_dir = repo.store.data_dir

    try:
        mainlines = load_mainlines(data_dir, as_of=as_of, reference_date=reference, top_n=USER_MAINLINE_TOP)
    except Exception:  # noqa: BLE001 - 主线只影响板块排序
        logger.warning("bellwether: mainline load failed", exc_info=True)
        mainlines = {"items": []}
    ranks = {(item["kind"], item["name"]): int(item["rank"]) for item in mainlines.get("items") or []
             if item.get("kind") and item.get("name") and item.get("rank") is not None}
    cfg = _filter_config()
    maps = {}
    for kind in SECTORS_PER_STOCK:
        try:
            maps[kind] = sector_map(_load_concept_map_df(repo, kind)[0], kind, cfg)
        except Exception:  # noqa: BLE001 - 某一维度缺失时只少该维度
            logger.warning("bellwether: %s membership unavailable", kind, exc_info=True)
    sectors, uncovered = pick_user_sectors(symbols, ctx["prev"], maps, ranks,
                                           excluded=_excluded_symbols(data_dir))
    if not sectors:
        return _user_unavailable(scope, "所选股票在上一交易日没有可用的板块归属", now, as_of)

    watched = list(dict.fromkeys([item["symbol"] for sector in sectors for item in sector["items"]]
                                 + [symbol for sector in sectors for symbol in sector["mine"]] + uncovered))
    rows = _quote_rows(current, watched)
    names = _names(repo, rows, watched)
    sealed = _sealed_map(depth_service, as_of)
    active = _recent_limit_ups(repo, ctx["sessions"], reference)
    notes = [MEMBERSHIP_NOTE]
    if active is None:
        notes.append("近期涨停历史不可用, 板块广度按全体成员计算")
    output = []
    for sector in sectors:
        items = [_track(entry, rows, names, sealed) for entry in sector["items"]]
        breadth = sector_breadth(current, sector["members"], active)
        result = verdict(role_signals(items, ("leader", "core"))
                         | {"breadth": breadth_signal(breadth)})
        if ctx["stale"]:
            result = _stale_verdict(result)
        mine = []
        for symbol in sector["mine"]:
            row = rows.get(symbol)
            change = _finite(row.get("change_pct")) if row else None
            mine.append({"symbol": symbol, "name": names.get(symbol) or symbol, "change_pct": change,
                         "state": stock_state(row, (sealed or {}).get(symbol)),
                         "relative": relative_strength(change, breadth["avg_pct"] if breadth else None)})
        listed = {item["symbol"] for item in items} | set(sector["mine"])
        fresh = _today_limit_ups(current, set(sector["members"]) - listed, sealed)[:MAX_SECTOR_NEW]
        output.append({key: sector[key] for key in ("kind", "name", "mainline_rank", "member_count",
                                                     "limit_up_prev", "avg_pct_prev")}
                      | {"verdict": result, "items": items, "breadth": breadth, "mine": mine,
                         "new_limit_ups": [{key: item[key] for key in ("symbol", "name", "boards_today")}
                                           for item in fresh]})
    return {
        "scope": scope, "status": "ready", "reason": "", **_stamp(now, ctx),
        "total": len(symbols), "sectors": output,
        "uncovered": [{"symbol": symbol, "name": names.get(symbol) or symbol} for symbol in uncovered],
        "notes": notes,
    }


_cache: dict[tuple, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def invalidate_cache() -> None:
    with _cache_lock:
        _cache.clear()


def get_snapshot(repo, *, scope: str = "market", group_id: str | None = None,
                 quote_service=None, depth_service=None) -> dict:
    """短 TTL 缓存的快照, 供接口轮询; 计算在锁外, 并发重算无副作用。

    持仓/自选每次请求都重读代码列表(很轻), 并把列表放进缓存键, 修改后立即生效。
    """
    key: tuple = ("market",)
    symbols: list[str] = []
    if scope in USER_SCOPES:
        try:
            symbols, reason = load_user_symbols(repo.store.data_dir, scope, group_id)
        except Exception:  # noqa: BLE001 - 自选/批次文件损坏时明确提示
            logger.warning("bellwether: %s symbols unavailable", scope, exc_info=True)
            symbols, reason = [], "持仓或自选数据读取失败, 请检查数据文件"
        if reason:
            return _user_unavailable(scope, reason, cn_now())
        key = (scope, group_id, tuple(symbols))
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < _CACHE_TTL:
            return hit[1]
    if scope in USER_SCOPES:
        snapshot = build_user_snapshot(repo, scope, symbols, quote_service=quote_service,
                                       depth_service=depth_service)
    else:
        snapshot = build_snapshot(repo, quote_service=quote_service, depth_service=depth_service)
    with _cache_lock:
        # 只保留各范围的最新一份, 避免自选反复修改时键无限增长
        for stale_key in [k for k in _cache if k[:2] == key[:2]]:
            _cache.pop(stale_key)
        _cache[key] = (time.monotonic(), snapshot)
    return snapshot
