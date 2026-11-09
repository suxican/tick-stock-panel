"""Offline stock-level replay of the market-game candidate rules.

Research only. It reuses the live snapshot preparation, per-stock eligibility and
rule functions, but replays them over past sessions with TODAY's concept
memberships and stock names (survivorship and look-ahead in membership and ST
status). It never writes reports, model samples or user data.

Two questions are kept apart:
- ``plan``: what the full 1.3.0 rules would have listed (market gate + allocation).
- ``pool:<mode>``: the stock filter of each mode applied every day, regardless of
  the market state, to measure stock selection separately from regime timing.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, time

import polars as pl

from app.market_time import CN_TZ
from app.services import market_game
from app.services.index_const import CORE_INDEX_SYMBOLS
from app.services.market_game_sentiment import SentimentArchive, gate_series
from app.services.market_game_snapshot import (
    _BASE_COLUMNS,
    _aggregate,
    _align_raw,
    _coverage_denominators,
    _current_sectors,
    _indices,
    _instruments,
    _prepare,
    _read,
    sector_member_counts,
    stock_rows,
)

WARMUP = 80          # sessions before each chunk so MA20/ATR/ret20/history_days are complete
LOOKAHEAD = 4        # sessions after each chunk for 3-day outcomes
CHUNK = 120          # target sessions per read
ROUND_TRIP_COST = 0.003
MODES = ("panic_repair", "trend_pullback")
LIMITATIONS = [
    "使用今天的概念成分、股票名称与ST状态回放过去，存在幸存者与成分前视偏差，只能看方向，不能当作可交易回测。",
    "成交模拟只用日线：次日开盘高于触发上限或低于失效价取消；开盘在区间内按开盘价、低开后上穿按触发下沿成交。未核验分钟均价、板块广度与市场门条件。",
    "T+1：入场日不能卖出；第2、3日开盘低于失效价按开盘价、盘中触及按失效价止损，触及压力价按压力价止盈；同日同时触及按止损计；一字跌停当日无法卖出顺延；最迟第3日收盘按收盘价计。",
    "收益扣除0.3%往返成本；不含滑点冲击、排队与流动性约束。各日候选持有期重叠，不是独立样本。",
    "基准为全市场等权收益（剔除单日绝对涨跌超过35%的记录）。",
]


def _session_days(repo, start: date, end: date) -> list[date]:
    rows = repo.execute_all("SELECT DISTINCT date FROM kline_enriched WHERE date <= ? ORDER BY date", [end])
    days = sorted(row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0])) for row in rows)
    return days


def _series(frame: pl.DataFrame) -> dict[str, dict]:
    """Per symbol arrays on one consistent adjusted scale within each segment."""
    result = {}
    columns = ["date", "open", "high", "low", "close", "raw_close", "raw_high", "raw_low",
               "signal_limit_up", "signal_limit_down", "_segment_key"]
    for (symbol,), part in frame.select("symbol", *columns).sort("date").group_by(["symbol"], maintain_order=True):
        data = part.to_dict(as_series=False)
        data["index"] = {day: i for i, day in enumerate(data["date"])}
        result[symbol] = data
    return result


def _trade(bars: dict, i: int, candidate: dict, ew: dict) -> dict:
    """Daily-bar approximation of the frozen plan; see LIMITATIONS."""
    scale = bars["raw_close"][i] / bars["close"][i]
    low_band, high_band = candidate["trigger_low"] / scale, candidate["trigger_high"] / scale
    stop, target = candidate["invalidation_price"] / scale, (candidate.get("pressure_price") or math.inf) / scale
    segment = bars["_segment_key"][i]
    days = []
    for step in (1, 2, 3):
        j = i + step
        if j >= len(bars["date"]) or bars["_segment_key"][j] != segment:
            break
        days.append(j)
    base_close = bars["close"][i]
    result = {"fwd1": None, "fwd3": None, "ew1": None, "ew3": None, "status": "no_data", "net": None, "exit": None}
    if days:
        result["fwd1"] = bars["close"][days[0]] / base_close - 1
        result["ew1"] = ew.get(bars["date"][days[0]])
    if len(days) == 3:
        result["fwd3"] = bars["close"][days[2]] / base_close - 1
        values = [ew.get(bars["date"][j]) for j in days]
        result["ew3"] = math.prod(1 + v for v in values) - 1 if None not in values else None
    if len(days) < 3:
        return result
    first = days[0]
    opened, high, low = bars["open"][first], bars["high"][first], bars["low"][first]
    one_price_up = bars["raw_high"][first] == bars["raw_low"][first] and bars["signal_limit_up"][first]
    if opened > high_band or opened < stop or one_price_up:
        result["status"] = "cancelled"
        return result
    if high < low_band or low > high_band:
        result["status"] = "not_triggered"
        return result
    entry = opened if opened >= low_band else low_band
    exit_price, how = None, "time"
    pending_stop = bars["close"][first] < stop  # structure broke on the entry day; sell at next open
    for j in days[1:]:
        o, h, lo = bars["open"][j], bars["high"][j], bars["low"][j]
        locked = bars["raw_high"][j] == bars["raw_low"][j] and bars["signal_limit_down"][j]
        if locked:
            continue
        if pending_stop or o <= stop:
            exit_price, how = o, "stop"
            break
        if lo <= stop:
            exit_price, how = stop, "stop"
            break
        if h >= target:
            exit_price, how = max(o, target), "target"
            break
    if exit_price is None:
        exit_price = bars["close"][days[2]]
    result.update(status="filled", exit=how, net=exit_price / entry - 1 - ROUND_TRIP_COST)
    return result


def _gate(level_by_day: dict, day: str) -> dict | None:
    if not level_by_day:
        return None
    score, level = level_by_day.get(day, (None, "unavailable"))
    return {"status": "ready", "as_of": day, "fetched_at": None, "point_in_time": False,
            "reason": "离线回放", "metrics": [], "loss_gate": {"score": score, "level": level}}


def replay(repo, *, start: date, end: date, sectors: dict | None = None,
           mainline_filter: dict | None = None, sentiment: dict | None = None,
           top: int = 5) -> list[dict]:
    """Return one row per (session, source, candidate) with forward outcomes."""
    days = _session_days(repo, start, end)
    targets = [day for day in days if start <= day <= end]
    if not targets:
        return []
    sectors = sectors if sectors is not None else _current_sectors(repo)
    counts = sector_member_counts(sectors)
    if mainline_filter is None:
        from app.services.preferences import get_mainline_filter_config

        mainline_filter = get_mainline_filter_config()
        mainline_filter["blacklist"] = sorted(set(mainline_filter["blacklist"]))
    gates = gate_series(sentiment) if sentiment else {}
    # Current ST names are treated as historical so limit prices are computable.
    instruments = _instruments(repo).with_columns(pl.lit(date(1990, 1, 1)).alias("metadata_date"))
    cutoff = datetime.combine(end, time(23, 59), tzinfo=CN_TZ)
    position = {day: i for i, day in enumerate(days)}
    risk = market_game.DEFAULT_RISK
    rows = []
    for offset in range(0, len(targets), CHUNK):
        chunk = targets[offset:offset + CHUNK]
        first, last = position[chunk[0]], position[chunk[-1]]
        window = days[max(0, first - WARMUP):min(len(days), last + LOOKAHEAD + 1)]
        frame = _read(repo, "kline_enriched", [*_BASE_COLUMNS, "quote_ts"], window[0], window[-1])
        raw = _read(repo, "kline_daily", ["symbol", "date", "close", "high", "low", "quote_ts"], window[0], window[-1])
        prepared = _prepare(_align_raw(frame, raw), window, instruments, cutoff)
        if prepared.is_empty():
            continue
        indices = _indices(repo, window, cutoff)
        denominators = _coverage_denominators(prepared.select("symbol", "date"), window)
        history = {row["date"]: row for row in _aggregate(prepared, indices, window, denominators)}
        ew = dict(prepared.filter(pl.col("change_pct").abs() <= 0.35).group_by("date")
                  .agg(pl.col("change_pct").mean()).iter_rows())
        bars = _series(prepared)
        by_day = prepared.partition_by("date", as_dict=True)
        for day in chunk:
            current = by_day.get((day,))
            iso = day.isoformat()
            if current is None or iso not in history:
                continue
            previous_day = days[position[day] - 1].isoformat() if position[day] else None
            metrics = history[iso]
            index_count = indices.get(day, {}).get("_index_count", 0)
            snapshot = {
                "as_of": iso, "quality": "ready" if index_count >= len(CORE_INDEX_SYMBOLS) else "limited",
                "metrics": metrics, "previous": history.get(previous_day, {}),
                "stocks": stock_rows(current, sectors, day, []),
                "mainline_filter": mainline_filter, "sector_member_counts": counts,
                "sentiment": _gate(gates, iso),
            }
            report = market_game.analyze_snapshot(snapshot, risk)
            state = report["market_state"]
            gate = (snapshot["sentiment"] or {}).get("loss_gate", {}).get("level")
            context = {"date": iso, "phase": state["phase"], "gate": gate,
                       "cap": report["allocation"]["total_cap"], "vetoed": bool(state["risk_vetoes"])}
            sources = [("plan", report["candidates"])]
            unique = market_game._stocks(snapshot)
            groups, _ = market_game._sectors(unique, metrics.get("median_return"), mainline_filter, counts)
            for mode in MODES:
                pool = market_game._candidate_pool(unique, groups, mode, risk)
                sources.append((f"pool:{mode}", pool))
                sources.append((f"top{top}:{mode}", pool[:top]))
            for source, candidates in sources:
                for candidate in candidates:
                    series = bars.get(candidate["symbol"])
                    i = series["index"].get(day) if series else None
                    if i is None:
                        continue
                    rows.append({**context, "source": source, "symbol": candidate["symbol"],
                                 "mode": candidate["mode"], "score": candidate.get("score"),
                                 "reward_risk": candidate.get("reward_risk_ratio"),
                                 "max_position": candidate.get("max_position"),
                                 **_trade(series, i, candidate, ew)})
    return rows


def _mean(values):
    return sum(values) / len(values) if values else None


def _quantile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summarize(rows: list[dict], *, split: date | None = None) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        keys = [row["source"], f"{row['source']}|year:{row['date'][:4]}"]
        if row.get("gate"):
            keys.append(f"{row['source']}|gate:{row['gate']}")
        if split is not None:
            keys.append(f"{row['source']}|{'train' if row['date'] < split.isoformat() else 'test'}")
        for key in keys:
            groups[key].append(row)
    result = []
    for key, members in sorted(groups.items()):
        fwd1 = [r["fwd1"] for r in members if r["fwd1"] is not None]
        fwd3 = [r["fwd3"] for r in members if r["fwd3"] is not None]
        ex1 = [r["fwd1"] - r["ew1"] for r in members if r["fwd1"] is not None and r["ew1"] is not None]
        ex3 = [r["fwd3"] - r["ew3"] for r in members if r["fwd3"] is not None and r["ew3"] is not None]
        filled = [r for r in members if r["status"] == "filled"]
        net = [r["net"] for r in filled]
        decided = [r for r in members if r["status"] != "no_data"]
        result.append({
            "group": key, "n": len(members), "dates": len({r["date"] for r in members}),
            "fwd1_mean": _mean(fwd1), "fwd3_mean": _mean(fwd3),
            "excess1_mean": _mean(ex1), "excess3_mean": _mean(ex3),
            "excess3_win": _mean([v > 0 for v in ex3]),
            "fill_rate": len(filled) / len(decided) if decided else None,
            "trade_n": len(filled), "trade_mean": _mean(net), "trade_median": _quantile(net, .5),
            "trade_win": _mean([v > 0 for v in net]), "trade_p10": _quantile(net, .1),
            "stop_rate": _mean([r["exit"] == "stop" for r in filled]),
            "target_rate": _mean([r["exit"] == "target" for r in filled]),
        })
    return result


def run_replay(repo, *, start: date, end: date, split: date | None = None, data_dir=None) -> dict:
    sentiment = None
    if data_dir is not None:
        try:
            sentiment = SentimentArchive(data_dir).rows() or None
        except ValueError:
            sentiment = None
    rows = replay(repo, start=start, end=end, sentiment=sentiment)
    return {
        "scope": "in_sample_stock_replay_current_membership", "rule_version": market_game.RULE_VERSION,
        "start": start.isoformat(), "end": end.isoformat(), "split": split.isoformat() if split else None,
        "groups": summarize(rows, split=split), "rows": rows, "limitations": LIMITATIONS,
    }
