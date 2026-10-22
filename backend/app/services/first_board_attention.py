"""Causal first-board attention evidence over caller-supplied normalized frames.

This module performs no IO and never changes a trading state or a buy gate.
Quote freshness belongs to the caller. Returns and breadth are fractions;
minute volume is lots and amount is yuan, exactly as in intraday_features.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta

import polars as pl

from app.market_time import CN_TZ
from app.strategy.intraday_features import build_feature_frame

RULE_VERSION = "attention-v1"
_MINUTE_COLUMNS = {"symbol", "datetime", "open", "high", "low", "close", "volume", "amount"}


def _tri_any(values: list[bool | None]) -> bool | None:
    if any(value is True for value in values):
        return True
    return None if not values or any(value is None for value in values) else False


def _finite(value: object) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def _at_limit(row: dict | None) -> bool | None:
    if not row:
        return None
    price, threshold = row.get("raw_close"), row.get("limit_up_price")
    if not _finite(price) or not _finite(threshold) or price <= 0 or threshold <= 0:
        return None
    # Runtime thresholds already include -0.005. A quote above the actual
    # exchange tick is invalid, rather than stronger evidence of a limit hit.
    if price > threshold + .005 + 1e-8:
        return None
    return price >= threshold


def _minute_unknown(reason: str) -> dict:
    return {"as_of": None, "return_5m": None, "return_15m": None,
            "max_drawdown": None, "above_vwap": None, "reason": reason,
            "_rising": None, "_window_start": None}


def _expected_session(start: datetime, end: datetime) -> list[datetime]:
    return [start + timedelta(minutes=i) for i in range(int((end - start).total_seconds() / 60) + 1)]


def _validate_minutes(rows: list[dict], now: datetime) -> str | None:
    if not rows:
        return "缺少当日已完成分钟"
    if "minute_price_basis" in rows[0] and any(row.get("minute_price_basis") != "raw" for row in rows):
        return "分钟价格基准缺失或混合，暂不计算分时联动"
    # Diagonal merges add nulls for a source that never declared the optional
    # grid. All-null means legacy 240-bar validation; a partial declaration is
    # still mixed evidence and must fail closed.
    declared_grid = any(row.get("minute_time_grid") is not None for row in rows)
    auction_grid = declared_grid and all(row.get("minute_time_grid") == "auction_end" for row in rows)
    if declared_grid and not auction_grid:
        return "分钟时间网格声明缺失或混合，无法确认分时"
    times = [row["datetime"] for row in rows]
    cutoff = now.replace(tzinfo=None, second=0, microsecond=0)
    opening = cutoff.replace(hour=9, minute=30)
    morning_end = cutoff.replace(hour=11, minute=30)
    afternoon = cutoff.replace(hour=13, minute=0)
    close = cutoff.replace(hour=15, minute=0)
    if not (opening < cutoff <= morning_end + timedelta(minutes=1)
            or afternoon < cutoff <= close + timedelta(minutes=1)):
        return "当前不在连续竞价分钟确认时段"
    if len(set(times)) != len(times):
        return "当日分钟时间重复，无法确认分时"
    if times[0] not in ({opening} if auction_grid else {opening, opening + timedelta(minutes=1)}):
        return "缺少开盘分钟，无法确认当日均价"
    # The repository preserves provider labels. Existing minute consumers use
    # both 09:30/13:00 (start) and 09:31/13:01 (end) labels. Select the complete
    # 120-bar morning grid from the explicit opening label, never from the
    # absence of 11:30; require the afternoon to use the same convention.
    offset = timedelta(minutes=1) if auction_grid else times[0] - opening
    morning_last = morning_end - timedelta(minutes=1) + offset
    last = cutoff - timedelta(minutes=1)
    expected = _expected_session(times[0], min(last, morning_last))
    if cutoff > afternoon:
        afternoon_times = [time for time in times if time >= afternoon]
        if not afternoon_times or afternoon_times[0] != afternoon + offset:
            return "午后开盘分钟缺失或与上午时间标签不一致"
        afternoon_last = close - timedelta(minutes=1) + offset
        expected += _expected_session(afternoon_times[0], min(last, afternoon_last))
    if times != expected:
        return "当日分钟缺根或更新滞后，无法确认完整分时与均价"
    for row in rows:
        if not all(_finite(row[key]) for key in _MINUTE_COLUMNS - {"symbol", "datetime"}):
            return "分钟价格或量额含无效值"
        low, high, volume, amount = row["low"], row["high"], row["volume"], row["amount"]
        if low <= 0 or high < low or not low <= row["open"] <= high or not low <= row["close"] <= high:
            return "分钟价格范围无效"
        if volume < 0 or amount < 0 or (volume == 0 and amount != 0):
            return "分钟成交量或成交额无效"
        if volume > 0 and not low - .011 <= amount / (volume * 100) <= high + .011:
            return "分钟量额与价格口径不一致"
    return None


def _intraday(minute_df: pl.DataFrame, symbols: set[str], now: datetime,
              minute_basis_raw: bool, minute_reason: str | None) -> dict[str, dict]:
    reason = minute_reason or ("分钟价格基准未确认，暂不计算分时联动" if not minute_basis_raw else "缺少有效分钟数据")
    output = {symbol: _minute_unknown(reason) for symbol in symbols}
    if not minute_basis_raw or minute_df.is_empty() or not _MINUTE_COLUMNS.issubset(minute_df.columns):
        return output
    if not isinstance(minute_df.schema["datetime"], pl.Datetime):
        return {symbol: _minute_unknown("分钟时间格式无效") for symbol in symbols}
    cutoff = now.replace(tzinfo=None, second=0, microsecond=0)
    dt = pl.col("datetime")
    if minute_df.schema["datetime"].time_zone:
        dt = dt.dt.convert_time_zone(CN_TZ.key).dt.replace_time_zone(None)
    try:
        frame = minute_df.with_columns(dt.alias("datetime")).filter(
            pl.col("symbol").is_in(symbols) & (pl.col("datetime").dt.date() == cutoff.date())
            & (pl.col("datetime") < cutoff)
        ).sort(["symbol", "datetime"])
    except pl.exceptions.PolarsError:
        return {symbol: _minute_unknown("分钟数据格式无效") for symbol in symbols}
    valid: dict[str, list[dict]] = {}
    for (symbol,), part in frame.partition_by("symbol", as_dict=True).items():
        rows = part.to_dicts()
        issue = _validate_minutes(rows, now)
        if issue:
            output[symbol] = _minute_unknown(issue)
        else:
            valid[symbol] = rows
    if not valid:
        return output
    features = build_feature_frame(frame.filter(pl.col("symbol").is_in(valid)), cutoff=cutoff)
    last_features = {row["symbol"]: row for row in features.group_by("symbol").tail(1).to_dicts()}
    for symbol, rows in valid.items():
        session = [row for row in rows if (row["datetime"].hour >= 13) == (cutoff.hour >= 13)]
        if len(session) < 6:
            output[symbol] = _minute_unknown("当前交易时段未积累完整5分钟窗口")
            continue
        window = session[-6:]
        closes = [row["close"] for row in window]
        ret = closes[-1] / closes[0] - 1
        ret15 = session[-1]["close"] / session[-16]["close"] - 1 if len(session) >= 16 else None
        average = sum(closes) / len(closes)
        slope = sum((i - 2.5) * (value - average) for i, value in enumerate(closes))
        peak, drawdown = closes[0], 0.
        for price in closes:
            peak = max(peak, price)
            drawdown = max(drawdown, 1 - price / peak)
        vwap = last_features[symbol]["vwap"]
        if not _finite(vwap) or vwap <= 0:
            output[symbol] = _minute_unknown("有效成交量不足，无法确认当日均价")
            continue
        above = closes[-1] > vwap
        rising = ret > 0 and slope > 0 and above and drawdown <= .01
        output[symbol] = {
            "as_of": window[-1]["datetime"].replace(tzinfo=CN_TZ).isoformat(),
            "return_5m": ret, "return_15m": ret15, "max_drawdown": drawdown,
            "above_vwap": above,
            "reason": "近5分钟抬升、位于当日均价上方且回撤受控" if rising else "分时抬升、均价或回撤条件未同时满足",
            "_rising": rising, "_window_start": window[0]["datetime"].replace(tzinfo=CN_TZ),
        }
    return output


def _quotes(current: pl.DataFrame) -> pl.DataFrame:
    empty = pl.DataFrame(schema={"symbol": pl.String, "change_pct": pl.Float64})
    if current.is_empty() or not {"symbol", "change_pct"}.issubset(current.columns):
        return empty
    if current.schema["change_pct"] == pl.Boolean:
        return empty
    try:
        return current.with_columns(
            pl.col("symbol").cast(pl.String, strict=False).str.strip_chars().str.to_uppercase(),
            pl.col("change_pct").cast(pl.Float64, strict=False),
        ).with_columns(pl.len().over("symbol").alias("_count")).filter(
            (pl.col("_count") == 1) & pl.col("change_pct").is_finite()
        ).drop("_count")
    except pl.exceptions.PolarsError:
        return empty


def _sectors(quotes: pl.DataFrame, groups: list[dict]) -> list[dict]:
    records, output = [], []
    for index, group in enumerate(groups):
        members = sorted(set(group["members"]))
        scope = (group["kind"], group["source_id"], group["field"])
        output.append({**{key: group[key] for key in ("id", "name", "kind", "source_id", "field")},
                       "rank": None, "total": 0, "member_count": len(members), "valid_count": 0,
                       "coverage": 0., "mean_change_pct": None, "market_mean_change_pct": None,
                       "up_ratio": None,
                       "strong": None, "follow": None, "reason": "有效成员不足5只或行情覆盖不足80%",
                       "leader": None, "_scope": scope, "_members": members})
        records.extend({"symbol": symbol, "_group": index, "_scope": "\x1f".join(scope)} for symbol in members)
    if not records or quotes.is_empty():
        return output
    joined = pl.DataFrame(records).join(quotes.select("symbol", "change_pct"), on="symbol", how="inner")
    markets = {row["_scope"]: row["market_mean"] for row in joined.unique(["_scope", "symbol"]).group_by("_scope").agg(
        pl.col("change_pct").mean().alias("market_mean")
    ).to_dicts()}
    for entry in output:
        entry["market_mean_change_pct"] = markets.get("\x1f".join(entry["_scope"]))
    stats = joined.group_by("_group").agg(
        pl.len().alias("valid_count"), pl.col("change_pct").mean().alias("mean_change_pct"),
        (pl.col("change_pct") > 0).mean().alias("up_ratio"),
    )
    scopes: dict[tuple, list[dict]] = defaultdict(list)
    for row in stats.to_dicts():
        entry = output[row.pop("_group")]
        entry.update(row, coverage=row["valid_count"] / entry["member_count"])
        if entry["valid_count"] >= 5 and entry["coverage"] >= .8:
            scopes[entry["_scope"]].append(entry)
    for scope, entries in scopes.items():
        ordered = sorted(entries, key=lambda item: -item["mean_change_pct"])
        rank, previous = 0, None
        for position, entry in enumerate(ordered, 1):
            mean = entry["mean_change_pct"]
            if mean != previous:
                rank = position
            previous = mean
            strong = (rank <= math.ceil(len(entries) * .2) and mean > 0
                      and mean > markets["\x1f".join(scope)] and entry["up_ratio"] >= .6)
            entry.update(rank=rank, total=len(entries), strong=strong,
                         reason="排名、上涨广度和相对同来源分类覆盖股票的强度达到实验门槛" if strong else "排名、上涨广度或相对同来源分类覆盖股票的强度未同时达标")
    return output


class AttentionEvaluator:
    """Single-worker evaluator. Its in-memory timestamps mean first *observed*
    touch, never first exchange touch or proof of an executable sealed order.
    The caller serializes evaluate; restarting intentionally loses observations.
    """

    def __init__(self) -> None:
        self._observed: dict[str, datetime] = {}
        self._last_now: datetime | None = None
        self._membership_version: str | None = None
        self._minute_frame: pl.DataFrame | None = None
        self._minute_key: tuple | None = None
        self._minute_features: dict[str, dict] = {}

    def evaluate(self, current: pl.DataFrame, groups: list[dict], candidate_symbols: list[str],
                 minute_df: pl.DataFrame, *, now: datetime, membership_version: str | None,
                 minute_reason: str | None = None, minute_basis_raw: bool = False) -> dict[str, dict]:
        """Fresh quotes are enriched (raw_close, limit_up_price, fraction change_pct).

        Groups contain resolved full symbols and stable source/field identities.
        Minute basis is explicit and defaults to unknown for legacy partitions.
        The caller must not mutate minute_df in place. A stable frame identity
        denotes an unchanged batch and avoids repeated per-stock minute work.
        """
        now = now.replace(tzinfo=CN_TZ) if now.tzinfo is None else now.astimezone(CN_TZ)
        if (self._last_now is None or now.date() != self._last_now.date() or now < self._last_now
                or membership_version != self._membership_version):
            self._observed.clear()
        self._last_now, self._membership_version = now, membership_version
        quotes = _quotes(current)
        lookup = {row["symbol"]: row for row in quotes.to_dicts()}
        limits = {symbol: _at_limit(row) for symbol, row in lookup.items()}
        for symbol, at_limit in limits.items():
            if at_limit:
                self._observed.setdefault(symbol, now)
        symbols = set(candidate_symbols)
        minute_key = (now.replace(second=0, microsecond=0), tuple(sorted(symbols)),
                      minute_basis_raw, minute_reason)
        if self._minute_frame is not minute_df or self._minute_key != minute_key:
            self._minute_features = _intraday(minute_df, symbols, now, minute_basis_raw, minute_reason)
            # Keep the frame alive: a recycled Python id must never hit this cache.
            self._minute_frame, self._minute_key = minute_df, minute_key
        intraday = self._minute_features
        sector_rows = _sectors(quotes, groups)
        by_symbol: dict[str, list[dict]] = defaultdict(list)
        for entry in sector_rows:
            for symbol in symbols.intersection(entry["_members"]):
                by_symbol[symbol].append(entry)
        output = {}
        for symbol in symbols:
            evidence = []
            minute = intraday[symbol]
            for sector in by_symbol[symbol]:
                item = {key: value for key, value in sector.items() if not key.startswith("_")}
                peers = [peer for peer in sector["_members"] if peer != symbol]
                observed = sorted((self._observed[peer], peer) for peer in peers if peer in self._observed)
                active = [(at, peer) for at, peer in observed if limits.get(peer) is True]
                known = [(at, peer) for at, peer in observed if limits.get(peer) is False]
                chosen = (active or known)[:1]
                if chosen:
                    at, leader = chosen[0]
                    item["leader"] = {"symbol": leader, "name": lookup[leader].get("name") or leader,
                                      "observed_limit_at": at.isoformat(), "at_limit": limits[leader]}
                if limits.get(symbol) is True or minute["_rising"] is False:
                    follow = False
                elif limits.get(symbol) is None:
                    follow = None
                elif active:
                    follow = (None if minute["_rising"] is None else active[0][0] < minute["_window_start"])
                else:
                    follow = None if any(limits.get(peer) is None for peer in peers) else False
                item["follow"] = follow
                evidence.append(item)
            strong = _tri_any([item["strong"] for item in evidence])
            follow = _tri_any([item["follow"] for item in evidence])
            joint = any(item["strong"] is True and item["follow"] is True for item in evidence)
            group = ("S1F1" if joint else None if strong is None or follow is None or (strong and follow)
                     else f"S{int(strong)}F{int(follow)}")
            priority = (2 if joint else 1 if strong or follow else
                        0 if strong is False and follow is False else None)
            known_count = sum(value is not None for value in (strong, follow, minute["_rising"]))
            reasons = []
            if not evidence:
                reasons.append("缺少可确认的行业或概念归属")
            if strong is None and evidence:
                reasons.append("部分板块成员行情不足，强势判断待确认")
            if minute["_rising"] is None:
                reasons.append(minute["reason"])
            if strong and follow and not joint:
                reasons.append("强势板块与领涨带动来自不同板块，未合并为双重满足")
            output[symbol] = {
                "schema_version": 1, "rule_version": RULE_VERSION, "as_of": now.isoformat(),
                "membership_version": membership_version,
                "status": "ready" if known_count == 3 else "unavailable" if known_count == 0 else "partial",
                "strong_sector": strong, "leader_follow": follow, "intraday_rising": minute["_rising"],
                "priority": priority, "group": group, "groups": evidence,
                "intraday": {key: value for key, value in minute.items() if not key.startswith("_")},
                "reasons": reasons,
            }
        return output
