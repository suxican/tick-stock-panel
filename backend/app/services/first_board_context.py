"""First-board environment and sector proxies over existing normalized data.

Loaders are for service warmup/cache refresh. The realtime path calls only the
pure sector reducer; it never reads historical files or contacts a provider.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import polars as pl

from app.services.regime_builder import STATE_LABELS, load_regime_history
from app.services.rps_rotation import _load_concept_map_df

logger = logging.getLogger(__name__)

SECTOR_PROXY_NOTE = (
    "采用当前行业二级归属的联动代理, 不等于课程中的精确题材主线; "
    "至少 3 只有效行情、2 只涨幅达到 5%、行业平均涨幅达到 1% 为工程实验门槛。"
)


def load_environment(
    data_dir: Path, *, as_of: date, reference_date: date, allowed_states: list[str],
) -> dict:
    """Use exactly the supplied candidate T-1 session, never the latest row."""
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
    return result


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
    except Exception as exc:  # Optional context fails closed without stopping quote polling.
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
    get_instruments = getattr(repo, "get_instruments", None)
    if callable(get_instruments):
        try:
            instruments = get_instruments()
            if instruments is not None and "symbol" in instruments.columns:
                for symbol in instruments["symbol"].drop_nulls().to_list():
                    key = str(symbol).strip().upper()
                    values = paths.get(key, set()) | paths.get(key.split(".", 1)[0], set())
                    if values:
                        paths[key] = values
        except Exception as exc:
            logger.warning("first-board industry symbol resolution unavailable (%s)", type(exc).__name__)
    return {symbol: sorted(values)[0] for symbol, values in sorted(paths.items())}


def evaluate_sectors(current: pl.DataFrame, members: dict[str, str]) -> dict[str, dict]:
    """Aggregate fresh normalized stock quotes (change_pct is a fraction).

    The caller owns timestamp freshness and current-session filtering. Missing
    and duplicated quotes never contribute breadth, strength, or a zero return.
    """
    normalized = {str(symbol).strip().upper(): sector for symbol, sector in members.items()
                  if isinstance(sector, str) and sector.strip()}
    sectors = sorted(set(normalized.values()))
    output = {sector: {
        "sector": sector, "count": 0, "up_count": 0, "strong_count": 0,
        "mean_change_pct": None, "confirmed": False, "reason": "缺少有效的当时行业行情",
        "proxy_note": SECTOR_PROXY_NOTE,
    } for sector in sectors}
    if not output or current.is_empty() or not {"symbol", "change_pct"}.issubset(current.columns):
        return output
    if current.schema["change_pct"] == pl.Boolean:
        return output
    member_frame = pl.DataFrame({"symbol": list(normalized), "sector": list(normalized.values())})
    try:
        quotes = current.select(
            pl.col("symbol").cast(pl.String, strict=False).str.strip_chars().str.to_uppercase(),
            pl.col("change_pct").cast(pl.Float64, strict=False),
        ).with_columns(pl.len().over("symbol").alias("_count")).filter(
            (pl.col("_count") == 1) & pl.col("change_pct").is_finite()
        ).join(member_frame, on="symbol", how="inner")
        if quotes.is_empty():
            return output
        stats = quotes.group_by("sector").agg(
            pl.len().alias("count"),
            (pl.col("change_pct") > 0).sum().alias("up_count"),
            (pl.col("change_pct") >= 0.05).sum().alias("strong_count"),
            pl.col("change_pct").mean().alias("mean_change_pct"),
        )
    except pl.exceptions.PolarsError:
        return output
    for row in stats.to_dicts():
        confirmed = row["count"] >= 3 and row["strong_count"] >= 2 and row["mean_change_pct"] >= 0.01
        output[row["sector"]].update(
            **row, confirmed=confirmed,
            reason="行业联动代理达到实验门槛" if confirmed else "行业有效样本数、强势家数或平均涨幅未达到实验门槛",
        )
    return output
