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
    "T+5/10/20为信号收盘至对应交易日收盘的复权观察；最大浮盈/浮亏取T+1至T+N复权最高/最低价相对信号收盘的极值，并包含基准零变化，不代表可成交收益。",
    "市场基准为同日沪深主板、创业板及科创板中完整观察窗口可计算股票的等权报价收益，逐窗列出有效数/信号日有效数。缺口和异常复权按扫描口径隔离，上市年龄及历史交易资格未匹配；这是事后可观察样本基准，非可交易组合。",
]
HORIZONS = (5, 10, 20)


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


def _price_observations(row, window, bars, events, invalid_from, baseline_changed, benchmarks):
    observations = []
    for horizon in HORIZONS:
        status = ("baseline_changed" if row["signal_revised"] or baseline_changed else
                  "no_calendar" if not window else _observed(window, bars, events, invalid_from, horizon))
        benchmark = (benchmarks or {}).get(horizon, {})
        item = {
            "horizon": horizon, "status": status, "return_pct": None, "mfe_pct": None, "mae_pct": None,
            "benchmark_status": benchmark.get("status", "no_calendar" if not window else
                                              "pending" if len(window) <= horizon else "unavailable"),
            "benchmark_return_pct": benchmark.get("return_pct"),
            "benchmark_sample_count": benchmark.get("sample_count", 0),
            "benchmark_universe_count": benchmark.get("universe_count", 0),
            "excess_return_pct": None,
        }
        if status == "ok":
            multiplier, high, low = 1., row["base_close"], row["base_close"]
            for day in window[1:horizon + 1]:
                multiplier *= events.get(day, 1.)
                high = max(high, bars[day]["high"] * multiplier)
                low = min(low, bars[day]["low"] * multiplier)
            item.update(
                return_pct=bars[window[horizon]]["close"] * multiplier / row["base_close"] - 1.,
                mfe_pct=high / row["base_close"] - 1., mae_pct=low / row["base_close"] - 1.,
            )
            if item["benchmark_status"] == "ok" and item["benchmark_return_pct"] is not None:
                item["excess_return_pct"] = item["return_pct"] - item["benchmark_return_pct"]
        observations.append(item)
    return observations


def evaluate(row, window, bars, events, names, listing, invalid_from, *, baseline_changed=False,
             benchmarks=None):
    shape = {"status": "pending", "return_pct": None}
    execution = {"status": "pending", "entry_date": str(window[1]) if len(window) > 1 else None,
                 "exit_date": str(window[2]) if len(window) > 2 else None,
                 "gross_return_pct": None, "net_return_pct": None,
                 "shares": None, "entry_price": None, "exit_price": None,
                 "buy_fee": None, "sell_fee": None, "entry_cost": None}
    row.update(shape_t5=shape, execution=execution)
    row["observations"] = _price_observations(
        row, window, bars, events, invalid_from, baseline_changed, benchmarks,
    )
    row["horizons_mature"] = len(window) > max(HORIZONS)
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


def market_benchmarks(service, requests, calendar, listings):
    """One bounded-symbol history pass shared by all requested dates/horizons.

    Full history is needed for the scanner's dated factor quarantine. Reading
    only the forward window could silently admit a previously quarantined stock.
    No benchmark universe is selected using the candidate's future outcome.
    """
    if not requests:
        return {}
    # Runtime import reuses the scanner universe without a module import cycle.
    from app.services.huichun_mode import _screening_stock

    indices = {day: i for i, day in enumerate(calendar)}
    targets = {(day, horizon): calendar[indices[day] + horizon]
               for day, horizons in requests.items() for horizon in horizons}
    end = max(targets.values())
    symbols = sorted({row[0] for row in service.repo.execute_all(
        "SELECT DISTINCT symbol FROM kline_daily WHERE date <= ?", [end],
    ) if _screening_stock(row[0])})
    counts, sums, universe = defaultdict(int), defaultdict(float), defaultdict(int)
    for offset in range(0, len(symbols), 512):
        service._check_stop()
        batch = symbols[offset:offset + 512]
        raw = service._raw(batch, end, start=calendar[0])
        factors = service._factors(batch, end)
        prepared, _ = service._prepare_scan_batch(
            raw, factors, calendar=calendar, end=end, listings=listings,
            expected_symbols=batch, calendar_verified=True,
        )
        frame = prepared.frame.sort("symbol", "date")
        if frame.is_empty():
            continue
        for day, count in frame.filter(pl.col("date").is_in(list(requests))).group_by("date").len().iter_rows():
            universe[day] += count
        groups = ["symbol", "segment_id"]
        for horizon in HORIZONS:
            dates = [day for day, h in targets if h == horizon]
            if not dates:
                continue
            observations = frame.with_columns(
                pl.col("date").shift(-horizon).over(groups).alias("_end"),
                (pl.col("close").shift(-horizon).over(groups) / pl.col("close") - 1.).alias("_return"),
            ).filter(pl.col("date").is_in(dates))
            for day, end_day, value in observations.select("date", "_end", "_return").iter_rows():
                if end_day == targets[day, horizon] and value is not None and math.isfinite(value):
                    counts[day, horizon] += 1
                    sums[day, horizon] += value
    result = defaultdict(dict)
    for day, horizon in targets:
        count = counts[day, horizon]
        result[day][horizon] = {
            "status": "ok" if count else "unavailable",
            "return_pct": sums[day, horizon] / count if count else None,
            "sample_count": count, "universe_count": universe[day],
        }
    return dict(result)


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
        if day >= recent_since or rescanned or not row.get("horizons_mature") or not row.get("observations"):
            groups[day[:7]].append(row)
    instruments = _instrument_facts(service.repo) if groups and official else {}
    requests = {}
    if official:
        for group in groups.values():
            for row in group:
                day = date.fromisoformat(row["signal_date"])
                idx = bisect_left(calendar, day)
                if idx < len(calendar) and calendar[idx] == day:
                    mature = tuple(h for h in HORIZONS if idx + h < len(calendar))
                    if mature:
                        requests[day] = mature
    benchmarks = market_benchmarks(
        service, requests, calendar, {symbol: facts[0] for symbol, facts in instruments.items() if facts[0]},
    )
    for group in groups.values():
        service._check_stop()
        valid_rows = []
        for row in group:
            day = date.fromisoformat(row["signal_date"])
            idx = bisect_left(calendar, day)
            if not official or idx == len(calendar) or calendar[idx] != day:
                evaluate(row, [], {}, {}, {}, None, None)
            else:
                valid_rows.append((row, calendar[idx:idx + max(HORIZONS) + 1]))
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
                     invalid_from, baseline_changed=changed, benchmarks=benchmarks.get(base))
    return {"records": sorted(records.values(), key=lambda r: (r["signal_date"], r["symbol"]),
                              reverse=True), "updated_at": now.isoformat()}
