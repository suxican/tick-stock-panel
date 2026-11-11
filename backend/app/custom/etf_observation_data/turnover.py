"""Turnover multiples for the fixed broad-ETF basket.

Reads only local ETF daily/minute partitions and the published share ledger;
a page request never queries an upstream source.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

from app.custom.etf_observation_data.ledger import BASKET
from app.custom.etf_observation_data.read_model import FUND_NAMES, LISTED, ObservationWindow
from app.market_time import CN_TZ, trading_minutes_elapsed_from_dt

AVG_SESSIONS = 20
# A same-time multiple needs a stable intraday profile; fewer complete days
# would let one unusual session dominate the denominator.
MIN_PROFILE_DAYS = 10
STRONG, MILD = 1.8, 1.3
FULL_DAY = 240
TAIL_START = 210  # 14:30 in elapsed trading minutes
RECENT_SESSIONS = 30
STALE_MINUTES = 10
_LOOKBACK = timedelta(days=75)  # >= 21 sessions even across the National Day holiday


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def read_daily(repo, end: date) -> pl.DataFrame:
    frames = []
    for symbol in BASKET:
        df = repo.get_etf_daily(symbol, end - _LOOKBACK, end, columns=["date", "close", "amount"])
        if not df.is_empty() and {"date", "close", "amount"} <= set(df.columns):
            frames.append(df.select(pl.lit(symbol).alias("symbol"), "date", "close", "amount"))
    if not frames:
        return pl.DataFrame(schema={"symbol": pl.String, "date": pl.Date, "close": pl.Float64, "amount": pl.Float64})
    return pl.concat(frames, how="vertical_relaxed")


def build_profiles(minute: pl.DataFrame, min_days: int = MIN_PROFILE_DAYS) -> dict[str, list[float] | None]:
    """Average cumulative share of the day's turnover at each elapsed trading minute (0..240)."""
    profiles: dict[str, list[float] | None] = dict.fromkeys(BASKET)
    if minute.is_empty():
        return profiles
    clock = pl.col("datetime").dt.hour().cast(pl.Int32) * 60 + pl.col("datetime").dt.minute().cast(pl.Int32)
    # Bars carry Beijing wall-clock end labels; the lunch break holds the morning total.
    elapsed = (
        pl.when(clock <= 570).then(0)
        .when(clock <= 690).then(clock - 570)
        .when(clock < 780).then(120)
        .when(clock <= 900).then(clock - 660)
        .otherwise(FULL_DAY)
    )
    buckets = (
        minute.filter(pl.col("symbol").is_in(BASKET) & pl.col("amount").is_not_null())
        .with_columns(day=pl.col("datetime").dt.date(), m=elapsed)
        .group_by("symbol", "day", "m").agg(pl.col("amount").sum())
    )
    days = buckets.group_by("symbol", "day").agg(
        total=pl.col("amount").sum(), last=pl.col("m").max()
    ).filter((pl.col("total") > 0) & (pl.col("last") >= FULL_DAY - 1))
    grid = days.select("symbol", "day", "total").join(
        pl.DataFrame({"m": list(range(FULL_DAY + 1))}, schema={"m": pl.Int32}), how="cross"
    )
    curves = (
        grid.join(buckets.with_columns(pl.col("m").cast(pl.Int32)), on=["symbol", "day", "m"], how="left")
        .sort("symbol", "day", "m")
        .with_columns(frac=pl.col("amount").fill_null(0).cum_sum().over("symbol", "day") / pl.col("total"))
        .group_by("symbol", "m").agg(pl.col("frac").mean(), n=pl.len())
        .sort("symbol", "m")
    )
    for symbol, group in curves.group_by("symbol"):
        symbol = symbol[0]
        if group["n"].min() >= min_days:
            profiles[symbol] = group["frac"].to_list()
    return profiles


_profile_lock = threading.Lock()
_profile_cache: OrderedDict[tuple, dict] = OrderedDict()


def cached_profiles(repo, dates: list[date]) -> dict[str, list[float] | None]:
    root = Path(repo.store.data_dir) / "kline_etf_minute"
    signature = []
    for day in dates:
        path = root / f"date={day.isoformat()}" / "part.parquet"
        try:
            stat = path.stat()
            signature.append((day, stat.st_mtime_ns, stat.st_size))
        except OSError:
            continue
    key = (str(root), tuple(signature))
    with _profile_lock:
        if key in _profile_cache:
            _profile_cache.move_to_end(key)
            return _profile_cache[key]
    available = [day for day, *_ in signature]
    minute = repo.get_minute_by_dates(list(BASKET), available, "etf") if len(available) >= MIN_PROFILE_DAYS else pl.DataFrame()
    profiles = build_profiles(minute)
    with _profile_lock:
        _profile_cache[key] = profiles
        while len(_profile_cache) > 4:
            _profile_cache.popitem(last=False)
    return profiles


def share_series(window: ObservationWindow) -> dict:
    """Daily listed-unit change value plus a unit index valued at fixed base-day NAVs.

    Holding NAV at the base day keeps price moves out of the index, so it only
    tracks net creation/redemption across funds with different unit prices.
    """
    series, base, units = [], None, None
    for day in window.days:
        rows = window.by_day.get(day, {})
        complete = len(rows) == len(BASKET)
        if complete and base is None:
            base = {s: (rows[s]["listed_units"], rows[s]["unit_nav_cny"]) for s in BASKET}
        index = None
        if complete and base:
            denominator = math.fsum(u * nav for u, nav in base.values())
            index = 100 * math.fsum(rows[s]["listed_units"] * base[s][1] for s in BASKET) / denominator
            units = (day, {s: rows[s] for s in BASKET})
        change = math.fsum(r[LISTED] for r in rows.values()) if complete else None
        series.append({"date": day, LISTED: change, "index": index})
    return {
        "series": series,
        "units_as_of": units[0] if units else None,
        "ledger": units[1] if units else None,
    }


def _level(ratio):
    if ratio is None:
        return None
    return "strong" if ratio >= STRONG else "mild" if ratio >= MILD else "calm"


def summarize(
    daily: pl.DataFrame,
    *,
    elapsed: float | None,
    profiles: dict[str, list[float] | None],
    ledger: dict[str, dict] | None,
    huijin: dict[str, int | None] | None = None,
    today_curves: dict[str, list[float] | None] | None = None,
) -> dict:
    """elapsed is None for a finished session; otherwise trading minutes elapsed at the quote time.

    ledger holds the latest verified share rows; today_curves is the settled
    session's own cumulative turnover curve, used only for the closing-half-hour share.
    """
    if daily.is_empty():
        return {"as_of": None, "funds": [], "totals": None, "conclusion": None}
    as_of = daily["date"].max()
    sessions = sorted(d for d in daily["date"].unique().to_list() if d < as_of)
    base_dates = sessions[-AVG_SESSIONS:] if len(sessions) >= AVG_SESSIONS else []
    recent_dates = [*sessions, as_of][-RECENT_SESSIONS:]
    previous = sessions[-1] if sessions else None
    by_key = {(r["symbol"], r["date"]): r for r in daily.iter_rows(named=True)}
    live = elapsed is not None and elapsed < FULL_DAY
    huijin = huijin or {}

    funds = []
    amounts, avgs, expecteds, sizes, moves, tails, tail_bases = [], [], [], [], [], [], []
    for symbol in BASKET:
        today = by_key.get((symbol, as_of), {})
        amount, close = today.get("amount"), today.get("close")
        amount = amount if _finite(amount) else None
        prev_close = by_key.get((symbol, previous), {}).get("close")
        history = [by_key.get((symbol, d), {}).get("amount") for d in base_dates]
        avg = (
            math.fsum(history) / len(history)
            if history and all(_finite(v) and v > 0 for v in history)
            else None
        )
        change = close / prev_close - 1 if _finite(close) and _finite(prev_close) and prev_close > 0 else None
        progress = amount / avg if amount is not None and avg else None
        expected = None
        if avg is not None:
            if not live:
                expected = avg
            elif profiles.get(symbol) and elapsed > 0:
                frac = profiles[symbol][min(int(elapsed), FULL_DAY)]
                expected = avg * frac if frac > 0 else None
        ratio = amount / expected if amount is not None and expected else None
        row = (ledger or {}).get(symbol) or {}
        units = row.get("listed_units")
        # Scale uses the latest verified listed units at today's price, not the fund's NAV.
        size = units * close if _finite(units) and _finite(close) else None
        curve, profile = (today_curves or {}).get(symbol), profiles.get(symbol)
        funds.append({
            "symbol": symbol,
            "name": FUND_NAMES[symbol],
            "amount_cny": amount,
            "avg_amount_cny": avg,
            "progress": progress,
            "ratio": ratio,
            "change_pct": change,
            "size_cny": size,
            "listed_units": units if _finite(units) else None,
            "listed_change_units": row.get("listed_change_units"),
            "huijin_units": huijin.get(symbol),
            "recent_amounts_cny": [by_key.get((symbol, d), {}).get("amount") for d in recent_dates],
        })
        amounts.append(amount)
        avgs.append(avg)
        expecteds.append(expected)
        sizes.append(size)
        moves.append(change)
        tails.append(amount * (1 - curve[TAIL_START]) if not live and curve and amount is not None else None)
        tail_bases.append(avg * (1 - profile[TAIL_START]) if not live and profile and avg is not None else None)

    # Basket totals are fail-closed: one missing fund leaves the aggregate unknown.
    def total(values):
        return math.fsum(values) if all(v is not None for v in values) else None

    amount_sum, avg_sum, expected_sum, size_sum = total(amounts), total(avgs), total(expecteds), total(sizes)
    tail_sum, tail_base_sum = total(tails), total(tail_bases)
    ratio = amount_sum / expected_sum if amount_sum is not None and expected_sum else None
    weighted_change = None
    if size_sum and all(m is not None for m in moves):
        weighted_change = math.fsum(w * m for w, m in zip(sizes, moves, strict=True)) / size_sum
    totals = {
        "amount_cny": amount_sum,
        "avg_amount_cny": avg_sum,
        "ratio": ratio,
        "progress": amount_sum / avg_sum if amount_sum is not None and avg_sum else None,
        "weighted_change_pct": weighted_change,
        "tail_share": tail_sum / amount_sum if tail_sum is not None and amount_sum else None,
        "tail_share_avg": tail_base_sum / avg_sum if tail_base_sum is not None and avg_sum else None,
    }
    return {
        "as_of": as_of.isoformat(),
        "previous_date": previous.isoformat() if previous else None,
        "baseline_start": base_dates[0].isoformat() if base_dates else None,
        "baseline_end": base_dates[-1].isoformat() if base_dates else None,
        "recent_dates": [d.isoformat() for d in recent_dates],
        "live": live,
        "elapsed_minutes": round(elapsed) if live else None,
        "funds": funds,
        "totals": totals,
        "conclusion": _level(ratio),
    }


def written_at(repo, as_of: date, now: datetime) -> datetime | None:
    """When today's ETF row was last written, or None for a past (settled) session.

    Realtime polling can stop for ETFs while the clock keeps running, so the
    partition write time, not the wall clock, says how much of the day it holds.
    """
    if as_of != now.date():
        return None
    path = Path(repo.store.data_dir) / "kline_etf_enriched" / f"date={as_of.isoformat()}" / "part.parquet"
    try:
        moment = datetime.fromtimestamp(path.stat().st_mtime, tz=CN_TZ)
    except OSError:
        moment = now
    return min(moment, now)


def snapshot(repo, now: datetime, ledger: dict[str, dict] | None, huijin: dict[str, int | None] | None = None) -> dict:
    daily = read_daily(repo, now.date())
    elapsed, profiles, moment, today_curves = None, {}, None, None
    if not daily.is_empty():
        as_of = daily["date"].max()
        moment = written_at(repo, as_of, now)
        if moment is not None:
            elapsed = trading_minutes_elapsed_from_dt(moment)
        sessions = sorted(d for d in daily["date"].unique().to_list() if d < as_of)
        profiles = cached_profiles(repo, sessions[-AVG_SESSIONS:])
        if elapsed is None or elapsed >= FULL_DAY:
            # build_profiles only keeps a day that reaches the close, so a cut-off feed yields no tail share.
            today_curves = build_profiles(repo.get_minute_batch(list(BASKET), as_of, "etf"), min_days=1)
    result = summarize(daily, elapsed=elapsed, profiles=profiles, ledger=ledger, huijin=huijin, today_curves=today_curves)
    partial = moment is not None and result["live"]
    result["data_time"] = moment.strftime("%H:%M") if partial else None
    # A partial row that stopped updating must not read as the current session;
    # trading minutes keep the lunch break from counting as a lag.
    result["stale"] = partial and trading_minutes_elapsed_from_dt(now) - elapsed > STALE_MINUTES
    return result


def huijin_units(disclosures: dict) -> dict[str, int]:
    """Units the two Huijin entities held per the last named annual-report disclosure.

    Named third-party asset-management plans are excluded: they are not Huijin's own holdings.
    """
    return {
        fund["symbol"]: sum(h["units"] for h in fund["holders"] if h["kind"] == "direct")
        for fund in disclosures.get("funds", [])
    }
