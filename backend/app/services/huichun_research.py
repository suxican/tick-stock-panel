"""Frozen A0 and near-threshold cohorts with separate price and fill observations."""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import numpy as np
import polars as pl

from app.price_limits import (
    is_no_limit_day,
    is_risk_warning_name,
    numpy_limit_price,
    parse_listing_date,
    price_limit_pct,
)
from app.services.first_board_portfolio import minimum_buy_qty, paper_order_block_reason
from app.services.huichun_audit import FACTOR_SCHEMA, _table_exists, prepare_batch
from app.strategy.huichun_a0 import _at_least
from app.strategy.paper import apply_slippage, buy_fee, sell_fee

LIMITATIONS = [
    "确认形态与仅因零轴距离被拒的外侧1个百分点样本分组存档，默认规则对应2%至3%。仅将当日15点后、收盘覆盖检查通过时首次登记的样本归入前瞻。",
    "T+5按信号收盘到第五个交易日收盘复权观察；开盘毛变化按T+1开盘至T+2开盘计算。历史资格及复权事件完整性待核验。",
    "净结果为独立单笔日线代理，每笔预算1万元、买卖各10基点不利滑点、佣金万三最低5元、卖出印花税按日期。无法确认限价或公司行动时留空。",
    "开盘涨停不买、跌停不卖，受阻不追单、不假定排队成交；缺口留空。未建模开盘成交量、仓位竞争和组合回撤。",
    "波动率为信号当日及以前、同一连续行情段内最近20个日收益率的样本标准差，未年化；不足21根收盘价时留空。",
]


def volatility_at_cross(indicators, crosses):
    if crosses.is_empty():
        return {}
    groups = ["symbol", "segment_id"]
    dates = crosses.select("symbol", pl.col("r_date").alias("date"))
    series = (
        indicators.select(*groups, "date", "close")
        .join(dates.select("symbol").unique(), on="symbol", how="semi")
        .sort(*groups, "date")
        .with_columns((pl.col("close") / pl.col("close").shift(1).over(groups) - 1).alias("r"))
        .with_columns(pl.col("r").rolling_std(20, min_samples=20, ddof=1)
                      .over(groups).alias("volatility_20"))
        .join(dates, on=["symbol", "date"], how="semi")
    )
    return {(s, d): v for s, d, v in series.select("symbol", "date", "volatility_20").iter_rows()}


def near_controls(cycles, *, start, threshold):
    """Only the zero-axis gate may have rejected this completed first cross."""
    return cycles.filter(
        (pl.col("r_date") >= start)
        & (pl.col("status") == "first_cross_rejected")
        & (pl.col("reasons") == "zero_distance_exceeded")
    ).filter(
        pl.col("zero_distance").map_elements(
            lambda value: _at_least(threshold + .01, value), return_dtype=pl.Boolean,
        )
    )


def register_rows(records, scan, controls, now: datetime):
    seen = set()
    stamp = now.isoformat()
    today = str(now.date())
    incomplete_day = today if (now.hour < 15 or
        scan.get("coverage", {}).get("publication_status") != "ready") else None
    for cohort, rows in (("confirmed", scan["candidates"]), ("zero_control", controls)):
        for row in rows:
            if row["signal_date"] == incomplete_day:
                continue  # A partial close cannot occupy or revise today's frozen sample.
            key = row["id"]
            seen.add(key)
            if key in records:
                old = records[key]
                if (old["baseline_fingerprint"] != row["baseline_fingerprint"]
                        or old["cohort"] != cohort
                        or any(old.get(field) != row.get(field)
                               for field in ("g_date", "d_date", "zero_distance", "rally_return",
                                             "volatility_20"))):
                    old["signal_revised"] = True
                continue
            symbol, day = row["symbol"], row["signal_date"]
            records[key] = {
                **row, "board": "gem" if symbol.startswith(("300", "301")) else
                "star" if symbol.startswith(("688", "689")) else "main",
                "cohort": cohort, "origin": "forward" if day == today else "backfill",
                "first_seen_at": stamp, "rules": scan["rules"],
                "rule_revision": scan["rule_revision"], "base_close": row["raw_close"],
                "signal_revised": False, "dated_names": {},
            }
    # A later historical repair can remove a whole cross from the new scan.
    for key, old in records.items():
        if (old["signal_date"] != incomplete_day and key not in seen
                and old["rule_revision"] == scan["rule_revision"]
                and old["rules"] == scan["rules"]
                and scan["start_date"] <= old["signal_date"] <= scan["end_date"]):
            old["signal_revised"] = True


def _observed(window, bars, events, invalid_from, horizon):
    observed = window[:horizon + 1]
    if invalid_from and observed and invalid_from <= observed[-1]:
        return "invalid_factor"
    if any(day not in bars for day in observed):
        return "unknown_gap"
    if len(observed) <= horizon:
        return "pending"
    multiplier = math.prod(events.get(day, 1.) for day in observed[1:])
    return "ok" if math.isfinite(multiplier) and multiplier > 0 else "invalid_factor"


def _open_proxy(row, window, bars, events, names, listing, execution):
    symbol = row["symbol"]
    for i, side in ((1, "buy"), (2, "sell")):
        if i == 2 and execution["status"] != "ok":
            return execution["status"]
        day = window[i]
        bar, previous = bars[day], bars[window[i - 1]]["close"]
        name = names.get(str(day))
        if (not name or "退" in name or not listing or listing > day or is_no_limit_day(symbol, listing, day)
                or paper_order_block_reason(symbol)):
            return "unknown_rules"
        # A split or distribution changes both the reference and share/cash entitlement.
        if events.get(day, 1.) != 1.:
            return "unverified_action"
        pct = price_limit_pct(symbol, day, is_risk_warning=is_risk_warning_name(name))
        ref, rate = np.array([previous]), np.array([pct])
        up = float(numpy_limit_price(ref, rate, up=True)[0])
        down = float(numpy_limit_price(ref, rate, up=False)[0])
        price = Decimal(str(apply_slippage(bar["open"], side, 10.))).quantize(
            Decimal(".01"), rounding=ROUND_CEILING if side == "buy" else ROUND_FLOOR,
        )
        fill = float(price)
        if bar["open"] > up + .005 or bar["open"] < down - .005:
            return "unknown_rules"
        if ((side == "buy" and (bar["open"] >= up - .005 or fill > up))
                or (side == "sell" and (bar["open"] <= down + .005 or fill < down))
                or fill > bar["high"] or fill < bar["low"]):
            return "entry_blocked" if side == "buy" else "exit_blocked"
        if side == "buy":
            qty = int(10000 / fill / 100) * 100
            if qty * fill + buy_fee(qty, fill, .0003) > 10000:
                qty -= 100
            if qty < minimum_buy_qty(symbol):
                return "insufficient_budget"
            commission = buy_fee(qty, fill, .0003)
            execution.update(shares=qty, entry_price=fill, buy_fee=commission,
                             entry_cost=qty * fill + commission)
        else:
            qty = execution["shares"]
            tax = .0005 if day >= date(2023, 8, 28) else .001
            sell_cost = sell_fee(qty, fill, .0003, tax)
            proceeds = qty * fill - sell_cost
            execution.update(net_return_pct=proceeds / execution["entry_cost"] - 1.,
                             exit_price=fill, sell_fee=sell_cost)
    return "ok"


def evaluate(row, window, bars, events, names, listing, invalid_from, *, baseline_changed=False):
    shape = {"status": "pending", "return_pct": None}
    execution = {"status": "pending", "entry_date": str(window[1]) if len(window) > 1 else None,
                 "exit_date": str(window[2]) if len(window) > 2 else None,
                 "gross_return_pct": None, "net_return_pct": None,
                 "shares": None, "entry_price": None, "exit_price": None,
                 "buy_fee": None, "sell_fee": None, "entry_cost": None}
    row.update(shape_t5=shape, execution=execution)
    row["horizons_mature"] = len(window) >= 6
    if row["signal_revised"] or baseline_changed:
        shape["status"] = execution["status"] = "baseline_changed"
        return
    if not window:
        shape["status"] = execution["status"] = "no_calendar"
        return
    shape["status"] = _observed(window, bars, events, invalid_from, 5)
    if shape["status"] == "ok":
        gain = bars[window[5]]["close"] / row["base_close"]
        shape["return_pct"] = gain * math.prod(events.get(d, 1.) for d in window[1:6]) - 1.
    execution["status"] = _observed(window, bars, events, invalid_from, 2)
    if execution["status"] == "ok":
        gross = bars[window[2]]["open"] / bars[window[1]]["open"] * events.get(window[2], 1.)
        execution["gross_return_pct"] = gross - 1.
    if _observed(window, bars, events, invalid_from, 1) == "ok":
        execution["status"] = _open_proxy(row, window, bars, events, names, listing, execution)


def _instrument_facts(repo):
    facts = {}
    if _table_exists(repo, "instruments"):
        columns = {r[0] for r in repo.execute_all("DESCRIBE instruments")}
        fields = [c if c in columns else "NULL" for c in ("listing_date", "as_of", "name")]
        for symbol, listing, as_of, name in repo.execute_all(
            f"SELECT symbol,{','.join(fields)} FROM instruments"
        ):
            # Duplicate evidence cannot authorize a fill for this symbol.
            facts[symbol] = ((parse_listing_date(listing), parse_listing_date(as_of), name)
                             if symbol not in facts else (None, None, None))
    return facts


def refresh(service, previous, scan, controls, now):
    """Re-evaluate bounded monthly cohorts; archived rows are never silently dropped."""
    records = {row["id"]: row for row in previous["records"]}
    if scan is not None:
        register_rows(records, scan, controls, now)
    if not records:
        return None
    calendar = service._calendar()
    official = service._calendar_source == "official_exchange_calendar"
    groups = defaultdict(list)
    recent_since = str(calendar[max(0, len(calendar) - 22)]) if calendar else ""
    for row in records.values():
        day = row["signal_date"]
        rescanned = (scan is not None and scan["start_date"] <= day <= scan["end_date"]
                     and row["rule_revision"] == scan["rule_revision"])
        if day >= recent_since or rescanned or not row.get("horizons_mature"):
            groups[day[:7]].append(row)
    instruments = _instrument_facts(service.repo) if groups and official else {}
    for group in groups.values():
        service._check_stop()
        valid_rows = []
        for row in group:
            day = date.fromisoformat(row["signal_date"])
            idx = bisect_left(calendar, day)
            if not official or idx == len(calendar) or calendar[idx] != day:
                evaluate(row, [], {}, {}, {}, None, None)
            else:
                valid_rows.append((row, calendar[idx:idx + 6]))
        if not valid_rows:
            continue
        start = min(window[0] for _, window in valid_rows)
        end = max(window[-1] for _, window in valid_rows)
        symbols = sorted({row["symbol"] for row, _ in valid_rows})
        raw = service._raw(symbols, end, start=start)
        factors = service._factors(symbols, end)
        sessions = calendar[bisect_left(calendar, start):bisect_right(calendar, end)]
        clean = prepare_batch(raw, pl.DataFrame(schema=FACTOR_SCHEMA), market_dates=sessions,
                              end=end, listing_dates={}).frame
        clean = raw.join(clean.select("symbol", "date"), on=["symbol", "date"], how="semi")
        bars, event_lookup = defaultdict(dict), defaultdict(dict)
        for bar in clean.iter_rows(named=True):
            bars[bar["symbol"]][bar["date"]] = bar
        for symbol, day, value in factors.iter_rows():
            event_lookup[symbol][day] = value
        invalid = service._factor_failure_dates(
            raw, factors.filter(pl.col("trade_date").is_null() | (pl.col("trade_date") >= start)),
            sessions,
        )
        raw_groups = raw.partition_by("symbol", as_dict=True)
        fac_groups = factors.partition_by("symbol", as_dict=True)
        for row, window in valid_rows:
            symbol = row["symbol"]
            listing, as_of, name = instruments.get(symbol, (None, None, None))
            names = row["dated_names"]
            if as_of in window[1:3] and isinstance(name, str) and name.strip():
                key = str(as_of)
                names[key] = name if names.get(key, name) == name else None
            base = window[0]
            changed = (base in bars[symbol] and row["baseline_fingerprint"] != service._baseline(
                raw_groups.get((symbol,), raw.head(0)),
                fac_groups.get((symbol,), factors.head(0)), symbol, base,
            ))
            invalid_from = invalid[symbol] or date.min if symbol in invalid else None
            evaluate(row, window, bars[symbol], event_lookup[symbol], names, listing,
                     invalid_from, baseline_changed=changed)
    return {"records": sorted(records.values(), key=lambda r: (r["signal_date"], r["symbol"]),
                              reverse=True), "updated_at": now.isoformat()}
