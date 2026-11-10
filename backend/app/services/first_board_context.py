"""First-board environment and sector proxies over existing normalized data.

Loaders are for service warmup/cache refresh. The realtime path calls only the
pure sector reducer; it never reads historical files or contacts a provider.
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import polars as pl

from app.services.market_mainline import _industry_member, load_mainline_history
from app.services.market_phase import PHASE_LABELS
from app.services.regime_builder import STATE_LABELS, load_regime_history
from app.services.rps_rotation import _load_concept_map_df

logger = logging.getLogger(__name__)
SECTOR_MIN_QUOTE_COVERAGE = 0.8
MAINLINE_TOP_N = 5
_LADDER_COLUMNS = ("max_consecutive", "first_board", "ge2_count", "promo_rate", "seal_rate",
                   "limit_up", "limit_down", "broken_limit")
_FULL_SYMBOL = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")

SECTOR_PROXY_NOTE = (
    "采用当前行业二级归属的联动代理, 不等于课程中的精确题材主线; "
    "剔除本股后, 同行完整成员行情覆盖至少 80%、至少 3 只有效同行行情、2 只同行涨幅达到 5%、"
    "同行平均涨幅达到 1% 为工程实验门槛。"
)


def load_environment(
    data_dir: Path, *, as_of: date, reference_date: date, allowed_states: list[str],
    allowed_phases: list[str] | None = None,
) -> dict:
    """Use exactly the supplied candidate T-1 session, never the latest row.

    allowed_phases 为 None 表示不按情绪阶段限制; 设置后阶段未知同样暂停提示。
    """
    result = {"date": str(reference_date) if type(reference_date) is date else None,
              "state": "unknown", "label": "未知", "allowed": False, "reason": ""}
    if type(as_of) is not date or type(reference_date) is not date or reference_date >= as_of:
        result["reason"] = "环境参考日必须是候选池对应的已完成交易日, 不能使用当日或未来收盘数据"
        return result
    if not isinstance(allowed_states, list) or any(
        not isinstance(state, str) or state not in STATE_LABELS for state in allowed_states
    ):
        result["reason"] = "允许的市场环境配置无效"
        return result
    history = load_regime_history(Path(data_dir))
    if history is None or history.is_empty() or not {"date", "state"}.issubset(history.columns):
        result["reason"] = "缺少市场环境历史, 暂停首板买入提示"
        return result
    try:
        date_column = (pl.col("date").str.to_date(strict=False)
                       if history.schema["date"] == pl.String else pl.col("date").cast(pl.Date, strict=False))
        matches = history.with_columns(date_column).filter(pl.col("date") == reference_date)
    except pl.exceptions.PolarsError:
        result["reason"] = "市场环境日期字段无效"
        return result
    if matches.height != 1:
        result["reason"] = ("环境记录重复, 无法确定参考值" if matches.height > 1 else
                            "缺少候选参考交易日的市场环境, 不使用更早或更新日期替代")
        return result
    state = matches["state"][0]
    if not isinstance(state, str) or state not in STATE_LABELS:
        result["reason"] = "参考交易日环境状态未知, 暂停首板买入提示"
        return result
    allowed = state in allowed_states
    result.update(state=state, label=STATE_LABELS[state], allowed=allowed,
                  reason="参考交易日市场环境符合当前设置" if allowed else "参考交易日市场环境不在允许范围")
    row = matches.row(0, named=True)
    phase = row.get("phase")
    phase = phase if isinstance(phase, str) and phase in PHASE_LABELS else None
    # 旧环境文件没有阶段与梯队列; 缺失保持 None, 不用零值冒充冰点。
    result.update(phase=phase, phase_label=PHASE_LABELS.get(phase, "未知"),
                  ladder={key: _finite(row.get(key)) for key in _LADDER_COLUMNS})
    if allowed_phases is not None and allowed:
        if phase is None:
            result.update(allowed=False, reason="参考交易日情绪阶段未知, 暂停首板买入提示")
        elif phase not in allowed_phases:
            result.update(allowed=False,
                          reason=f"参考交易日情绪阶段为{PHASE_LABELS[phase]}, 不在允许范围")
    return result


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def load_mainlines(data_dir: Path, *, as_of: date, reference_date: date | None,
                   top_n: int = MAINLINE_TOP_N) -> dict:
    """参考交易日已落盘的主线排行前 top_n(概念、行业各自排名), 不使用更早或更新日期替代。"""
    result = {"date": str(reference_date) if type(reference_date) is date else None,
              "status": "unavailable", "reason": "", "top_n": top_n, "items": []}
    if type(as_of) is not date or type(reference_date) is not date or reference_date >= as_of:
        result["reason"] = "主线参考日必须是已完成交易日"
        return result
    items = []
    for kind in ("concept", "industry"):
        history = load_mainline_history(Path(data_dir), kind)
        if history.is_empty() or not {"date", "member", "rank"}.issubset(history.columns):
            continue
        try:
            day = history.filter((pl.col("date").cast(pl.Date, strict=False) == reference_date)
                                 & (pl.col("rank") <= top_n)).sort("rank")
        except pl.exceptions.PolarsError:
            continue
        for row in day.iter_rows(named=True):
            name = row.get("member")
            if not isinstance(name, str) or not name.strip():
                continue
            items.append({"kind": kind, "name": name, "rank": int(row["rank"]),
                          **{key: _finite(row.get(key)) for key in
                             ("score", "limit_up_count", "ge2_count", "max_boards")},
                          "leader_symbol": row.get("leader_symbol")})
    if not items:
        result["reason"] = "缺少参考交易日的主线排行, 请在市场环境页确认主线已计算"
        return result
    result.update(status="ready", items=items)
    return result


def load_mainline_members(repo, mainlines: dict) -> dict[str, list[dict]]:
    """股票 → 命中的主线; 成分口径与主线计算一致(当前快照, 行业取前两级)。"""
    output: dict[str, list[dict]] = defaultdict(list)
    for kind in ("concept", "industry"):
        wanted = {item["name"]: item for item in mainlines.get("items", []) if item["kind"] == kind}
        if not wanted:
            continue
        mapping, _ = _load_concept_map_df(repo, kind)
        if mapping.is_empty() or not {"_sym_up", kind}.issubset(mapping.columns):
            continue
        seen = set()
        for symbol, member in mapping.select("_sym_up", kind).iter_rows():
            if not isinstance(symbol, str) or not isinstance(member, str):
                continue
            item = wanted.get(_industry_member(member, kind))
            key = (symbol.strip().upper(), kind, item["name"]) if item else None
            if key and key not in seen:
                seen.add(key)
                output[key[0]].append(item)
    return {symbol: sorted(items, key=lambda item: (item["rank"], item["kind"]))
            for symbol, items in output.items()}


def mainline_evidence(mainlines: dict, members: dict[str, list[dict]], symbol: str) -> dict:
    """hit 为 None 表示主线数据缺失, 不能当作未命中。"""
    if mainlines.get("status") != "ready":
        return {"hit": None, "date": mainlines.get("date"), "matches": [],
                "reason": mainlines.get("reason") or "主线数据暂不可用"}
    matches = [{key: item[key] for key in ("kind", "name", "rank", "limit_up_count", "max_boards")}
               for item in members.get(symbol, [])]
    top = mainlines.get("top_n", MAINLINE_TOP_N)
    return {"hit": bool(matches), "date": mainlines.get("date"), "matches": matches,
            "reason": (f"属于参考交易日主线：{'、'.join(match['name'] for match in matches)}" if matches
                       else f"不属于参考交易日概念或行业主线前 {top}")}


def _industry_level_two(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parts = [part.strip() for part in value.split("-") if part.strip()]
    if not parts or any(part.lower() in {"none", "null", "nan", "n/a"} for part in parts):
        return None
    # Keep the parent name to avoid merging homonymous subindustries.
    return "-".join(parts[:2])


def load_sector_members(repo) -> dict[str, str]:
    """Reuse the cached industry map; deterministic first path for multiple memberships.

    Code-only aliases are resolved with the repository instruments, never by
    guessing an exchange from the leading digit. These are current memberships
    and must not be injected into historical first-board research.
    """
    try:
        mapping, _ = _load_concept_map_df(repo, "industry")
    except Exception as exc:  # noqa: BLE001 - 行业上下文失败不阻断其他行情功能
        logger.warning("first-board industry mapping unavailable (%s)", type(exc).__name__)
        return {}
    if mapping.is_empty() or not {"_sym_up", "industry"}.issubset(mapping.columns):
        return {}
    paths: dict[str, set[str]] = {}
    for symbol, value in mapping.select("_sym_up", "industry").iter_rows():
        if not isinstance(symbol, str) or not symbol.strip():
            continue
        sector = _industry_level_two(value)
        if sector:
            paths.setdefault(symbol.strip().upper(), set()).add(sector)
    aliases: dict[str, set[str]] = defaultdict(set)
    get_instruments = getattr(repo, "get_instruments", None)
    if callable(get_instruments):
        try:
            instruments = get_instruments()
            if instruments is not None and "symbol" in instruments.columns:
                for symbol in instruments["symbol"].drop_nulls().to_list():
                    key = str(symbol).strip().upper()
                    if _FULL_SYMBOL.fullmatch(key):
                        aliases[key.split(".", 1)[0]].add(key)
        except Exception as exc:  # noqa: BLE001 - 缺少股票映射时仅保留已确认的标的
            logger.warning("first-board industry symbol resolution unavailable (%s)", type(exc).__name__)
    resolved: dict[str, set[str]] = defaultdict(set)
    unresolved_sectors = set()
    for symbol, values in paths.items():
        if _FULL_SYMBOL.fullmatch(symbol):
            resolved[symbol].update(values)
        elif len(aliases.get(symbol, set())) == 1:
            resolved[next(iter(aliases[symbol]))].update(values)
        else:
            # Dropping only the unknown member would artificially improve coverage.
            unresolved_sectors.update(values)
    if unresolved_sectors:
        logger.warning("first-board industry mapping excluded %d unresolved sectors", len(unresolved_sectors))
    primary = {symbol: sorted(values)[0] for symbol, values in sorted(resolved.items()) if values}
    return {symbol: sector for symbol, sector in primary.items() if sector not in unresolved_sectors}


def _member_quotes(current: pl.DataFrame, normalized: dict[str, str]) -> pl.DataFrame | None:
    """Unique finite member quotes; missing or duplicated quotes are never counted."""
    if current.is_empty() or not {"symbol", "change_pct"}.issubset(current.columns):
        return None
    if current.schema["change_pct"] == pl.Boolean:
        return None
    member_frame = pl.DataFrame({"symbol": list(normalized), "sector": list(normalized.values())},
                                schema={"symbol": pl.String, "sector": pl.String})
    try:
        return current.select(
            pl.col("symbol").cast(pl.String, strict=False).str.strip_chars().str.to_uppercase(),
            pl.col("change_pct").cast(pl.Float64, strict=False),
        ).with_columns(pl.len().over("symbol").alias("_count")).filter(
            (pl.col("_count") == 1) & pl.col("change_pct").is_finite()
        ).join(member_frame, on="symbol", how="inner").drop("_count")
    except pl.exceptions.PolarsError:
        return None


def evaluate_sector_peers(current: pl.DataFrame, members: dict[str, str],
                          symbols: Iterable[str]) -> dict[str, dict]:
    """Per-candidate industry confirmation over peers, excluding the candidate itself.

    The candidate is already near limit-up when this gate runs; counting its own
    quote would let it fill the strong-member count and lift the mean. Peer
    stats are the whole-sector aggregates minus the candidate's own counted
    quote. change_pct is a fraction; the caller owns timestamp freshness.
    """
    normalized = {str(symbol).strip().upper(): sector for symbol, sector in members.items()
                  if isinstance(sector, str) and sector.strip()}
    wanted = sorted({str(symbol).strip().upper() for symbol in symbols} & normalized.keys())
    if not wanted:
        return {}
    member_counts = Counter(normalized.values())
    totals: dict[str, dict] = {}
    own: dict[str, float] = {}
    quotes = _member_quotes(current, normalized)
    if quotes is not None and not quotes.is_empty():
        totals = {row["sector"]: row for row in quotes.group_by("sector").agg(
            pl.len().alias("count"),
            (pl.col("change_pct") > 0).sum().alias("up_count"),
            (pl.col("change_pct") >= 0.05).sum().alias("strong_count"),
            pl.col("change_pct").sum().alias("change_sum"),
        ).to_dicts()}
        own = dict(quotes.filter(pl.col("symbol").is_in(wanted)).select("symbol", "change_pct").iter_rows())
    output = {}
    for symbol in wanted:
        sector = normalized[symbol]
        total = totals.get(sector, {})
        change = own.get(symbol)
        counted = change is not None
        count = total.get("count", 0) - counted
        up_count = total.get("up_count", 0) - (counted and change > 0)
        strong_count = total.get("strong_count", 0) - (counted and change >= 0.05)
        member_count = member_counts[sector] - 1
        coverage = count / member_count if member_count else 0.0
        mean = (total.get("change_sum", 0.0) - (change if counted else 0.0)) / count if count else None
        covered = member_count > 0 and coverage >= SECTOR_MIN_QUOTE_COVERAGE
        confirmed = covered and count >= 3 and strong_count >= 2 and mean is not None and mean >= 0.01
        detail = f"同行有效行情 {count}/{member_count}，覆盖率 {coverage:.1%}"
        reason = ("剔除本股后行业内没有其他成员，无法确认同行联动" if not member_count else
                  f"剔除本股后行业同行联动达到实验门槛（{detail}）" if confirmed else
                  f"剔除本股后行业同行行情覆盖率不足 80%（{detail}）" if not covered else
                  f"剔除本股后行业同行有效样本数、强势家数或平均涨幅未达到实验门槛（{detail}）")
        output[symbol] = {
            "sector": sector, "count": count, "up_count": up_count, "strong_count": strong_count,
            "member_count": member_count, "coverage": coverage, "mean_change_pct": mean,
            "confirmed": confirmed, "excludes_candidate": True, "reason": reason,
            "proxy_note": SECTOR_PROXY_NOTE,
        }
    return output
