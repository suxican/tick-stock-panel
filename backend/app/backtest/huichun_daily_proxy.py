"""Explicit daily approximation for Huichun, separate from minute execution.

Unknown eligibility is assumed, missing entries cancel, held gaps retain capital.
Ex-factors adjust synthetic units, NOT actual shares or dividend cash settlement.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from math import floor, isfinite

import numpy as np
import polars as pl

from app.backtest.huichun_execution import order_fees, round_to_tick


def _limit(reference: float, multiplier: str) -> float:
    return float(
        (Decimal(str(reference)) * Decimal(multiplier)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )


def _fill_price(row: dict | None, side: str, slippage_bps: float) -> tuple[float | None, str]:
    if row is None:
        return None, "missing_daily_bar"
    ref = row.get("reference_close")
    if ref is None or not isfinite(ref) or ref <= 0:
        return None, "missing_previous_market_day"
    upper, lower = _limit(ref, "1.10"), _limit(ref, "0.90")
    if (side == "buy" and row["open"] >= upper - 1e-8) or (
        side == "sell" and row["open"] <= lower + 1e-8
    ):
        return None, "opening_limit_block"
    signed = 1 if side == "buy" else -1
    price = round_to_tick(row["open"] * (1 + signed * slippage_bps / 10000), 0.01, side)
    if not max(row["low"], lower) - 1e-8 <= price <= min(row["high"], upper) + 1e-8:
        return None, "slipped_price_outside_range"
    return price, "filled"


def simulate_daily_proxy(
    signals: pl.DataFrame,
    daily: pl.DataFrame,
    market_dates: list[date],
    *,
    start: date,
    end: date,
    adjustments: pl.DataFrame | None = None,
    slippage_bps: float = 5,
    risk_exit: bool = False,
    initial_cash: float = 1_000_000,
    max_positions: int = 5,
    target_fraction: float = 0.2,
    hold_days: int = 10,
) -> dict:
    """Run an opening-price proxy; all entry selection occurs before that open.

    daily contains raw OHLC, reference_close, prior_volume_shares, rank_amount,
    adj_close and ma60. Volumes are explicit shares. Factor events may fall on
    missing quote days. Splits restart from cash, and never inspect beyond end.
    """
    if start > end or initial_cash <= 0 or not isfinite(initial_cash):
        raise ValueError("Invalid range or capital")
    if hold_days < 1 or max_positions < 1 or not 0 < target_fraction <= 1:
        raise ValueError("Invalid holding or allocation parameters")
    if not isfinite(slippage_bps) or not 0 <= slippage_bps < 10000:
        raise ValueError("Invalid slippage")
    calendar = sorted(set(market_dates))
    index = {day: i for i, day in enumerate(calendar)}
    days = [day for day in calendar if start <= day <= end]
    if not days:
        raise ValueError("No trading dates")
    in_range_signals = signals.filter(pl.col("r_date").is_between(start, end))
    needed = in_range_signals["symbol"].unique().to_list()
    daily = daily.filter(pl.col("date").is_between(start, end) & pl.col("symbol").is_in(needed))
    if daily.select(pl.struct("symbol", "date").is_duplicated().any()).item():
        raise ValueError("Duplicate daily bars")
    bars = {(r["symbol"], r["date"]): r for r in daily.iter_rows(named=True)}
    events = defaultdict(dict)
    if adjustments is not None:
        for row in adjustments.filter(pl.col("trade_date") <= end).iter_rows(named=True):
            factor = row["ex_factor"]
            if not isfinite(factor) or factor <= 0:
                raise ValueError("Invalid event factor")
            day, symbol = row["trade_date"], row["symbol"]
            if symbol in events[day]:
                raise ValueError("Duplicate event factor")
            events[day][symbol] = factor
    by_signal_day = defaultdict(list)
    seen = set()
    stock_days = set()
    for r in in_range_signals.iter_rows(named=True):
        key = (r["symbol"], r["g_date"], r["d_date"])
        if key in seen or (r["symbol"], r["r_date"]) in stock_days:
            raise ValueError("Duplicate cycle signal")
        if r["r_date"] not in index:
            raise ValueError("Signal date absent from market calendar")
        seen.add(key)
        stock_days.add((r["symbol"], r["r_date"]))
        by_signal_day[r["r_date"]].append(r)
    cash, prior_nav, peak = float(initial_cash), float(initial_cash), float(initial_cash)
    positions, scheduled = {}, defaultdict(list)
    orders, trades, curve = [], [], []
    fills, turnover, total_fees = 0, 0.0, 0.0
    factor_applications, stale_days, delayed_days = 0, 0, 0
    for day in days:
        day_i = index[day]
        for symbol, pos in positions.items():
            factor = events[day].get(symbol, 1.0)
            if factor != 1.0:
                pos["units"] *= factor
                pos["last_price"] /= factor
                pos["factor_events"] += 1
                factor_applications += 1
        # Freeze selection before same-window sales: no sale proceeds or freed slots.
        available, slots = cash, max_positions - len(positions)
        plans = []
        for request in sorted(scheduled[day], key=lambda r: (-r["score"], r["symbol"])):
            reason = None
            if request["symbol"] in positions:
                reason = "already_held"
            elif slots <= 0:
                reason = "positions_full"
            elif available <= 0:
                reason = "no_cash"
            if reason:
                orders.append({**request, "execution_date": day, "status": reason, "budget": 0.0})
                continue
            budget = min(request["target_budget"], available)
            available -= budget
            slots -= 1
            plans.append((request, budget))
        for symbol in list(positions):
            pos = positions[symbol]
            if (
                pos["exit_signal_date"] is None
                or day <= pos["exit_signal_date"]
                or day_i <= pos["entry_index"]
            ):
                continue
            row = bars.get((symbol, day))
            price, reason = _fill_price(row, "sell", slippage_bps)
            cap = floor((row.get("prior_volume_shares") or 0) * 0.01 / 100) * 100 if row else 0
            if price is None or cap <= 0:
                pos["delayed_days"] += 1
                pos["last_exit_block"] = reason if price is None else "capacity_zero"
                delayed_days += 1
                continue
            quantity = min(pos["units"], cap)
            notional = round(quantity * price, 2)
            fee = order_fees(notional, day, "sell")
            cash = round(cash + notional - fee, 2)
            total_fees += fee
            turnover += notional
            pos["net_proceeds"] += notional - fee
            pos["units"] -= quantity
            pos["sell_fees"] += fee
            pos["sell_attempt_fills"] += 1
            if pos["units"] <= 1e-8:
                pnl = pos["net_proceeds"] - pos["entry_cost"]
                trades.append(
                    {
                        "symbol": symbol,
                        "signal_date": pos["signal_date"],
                        "entry_date": pos["entry_date"],
                        "exit_date": day,
                        "entry_price": pos["entry_price"],
                        "last_exit_price": price,
                        "entry_cost": pos["entry_cost"],
                        "net_proceeds": pos["net_proceeds"],
                        "pnl": pnl,
                        "return_pct": pnl / pos["entry_cost"],
                        "holding_days": day_i - pos["entry_index"] + 1,
                        "exit_reason": pos["exit_reason"],
                        "delayed_days": pos["delayed_days"],
                        "factor_events": pos["factor_events"],
                        "fees": pos["buy_fee"] + pos["sell_fees"],
                    }
                )
                del positions[symbol]
            else:
                pos["delayed_days"] += 1
                delayed_days += 1
        for request, budget in plans:
            symbol = request["symbol"]
            row = bars.get((symbol, day))
            price, reason = _fill_price(row, "buy", slippage_bps)
            quantity, fee = 0, 0.0
            if price is not None:
                cap = floor((row.get("prior_volume_shares") or 0) * 0.01 / 100) * 100
                quantity = min(cap, floor(budget / price / 100) * 100)
                while quantity > 0:
                    fee = order_fees(round(quantity * price, 2), day, "buy")
                    if quantity * price + fee <= min(cash, budget) + 1e-8:
                        break
                    quantity -= 100
                if quantity <= 0:
                    reason = "capacity_or_budget_too_small"
                    fee = 0.0
            orders.append(
                {
                    **request,
                    "execution_date": day,
                    "status": reason,
                    "budget": budget,
                    "shares": quantity,
                    "price": price,
                    "fee": fee,
                }
            )
            if quantity <= 0 or price is None:
                continue
            notional = round(quantity * price, 2)
            cost = notional + fee
            cash = round(cash - cost, 2)
            if cash < -1e-6:
                raise RuntimeError("Negative cash")
            total_fees += fee
            turnover += notional
            fills += 1
            positions[symbol] = {
                "symbol": symbol,
                "signal_date": request["signal_date"],
                "entry_date": day,
                "entry_index": day_i,
                "entry_price": price,
                "entry_cost": cost,
                "entry_notional": notional,
                "units": float(quantity),
                "last_price": price,
                "last_price_date": day,
                "net_proceeds": 0.0,
                "buy_fee": fee,
                "sell_fees": 0.0,
                "exit_signal_date": None,
                "exit_reason": None,
                "delayed_days": 0,
                "factor_events": 0,
                "sell_attempt_fills": 0,
            }
        stale_value, market_value = 0.0, 0.0
        for symbol, pos in positions.items():
            row = bars.get((symbol, day))
            if row is not None:
                pos["last_price"], pos["last_price_date"] = row["close"], day
            value = pos["units"] * pos["last_price"]
            market_value += value
            if pos["last_price_date"] != day:
                stale_value += value
                stale_days += 1
            if pos["exit_signal_date"] is not None:
                continue
            reason = None
            if risk_exit and row is not None:
                gain = (value + pos["net_proceeds"]) / pos["entry_notional"] - 1
                if gain <= -0.07:
                    reason = "stop_loss_close"
                elif row.get("ma60") is not None and row["adj_close"] < row["ma60"]:
                    reason = "below_ma60_close"
                elif gain >= 0.15:
                    reason = "take_profit_close"
            if reason is None and day_i - pos["entry_index"] + 1 >= hold_days:
                reason = "holding_period"
            if reason is not None:
                pos["exit_signal_date"], pos["exit_reason"] = day, reason
        nav = cash + market_value
        peak = max(peak, nav)
        curve.append(
            {
                "date": day,
                "equity": nav,
                "cash": cash,
                "market_value": market_value,
                "exposure": market_value / nav if nav else 0.0,
                "drawdown": nav / peak - 1,
                "positions": len(positions),
                "stale_value": stale_value,
            }
        )
        for signal in by_signal_day[day]:
            row = bars.get((signal["symbol"], day))
            request = {
                "symbol": signal["symbol"],
                "signal_date": day,
                "g_date": signal["g_date"],
                "d_date": signal["d_date"],
                "target_budget": prior_nav * target_fraction,
                "score": float(row.get("rank_amount") or 0) if row else 0.0,
            }
            if day_i + 1 >= len(calendar) or calendar[day_i + 1] > end:
                orders.append(
                    {**request, "execution_date": None, "status": "end_censored", "budget": 0.0}
                )
            else:
                scheduled[calendar[day_i + 1]].append(request)
        prior_nav = nav
    values = np.array([initial_cash, *[r["equity"] for r in curve]])
    returns = values[1:] / values[:-1] - 1
    pnl = np.array([r["pnl"] for r in trades])
    per_trade = np.array([r["return_pct"] for r in trades])
    final = curve[-1]
    stats = {
        "model": "A0_DAILY_PROXY",
        "start": str(start),
        "end": str(end),
        "risk_exit": risk_exit,
        "slippage_bps": slippage_bps,
        "initial_cash": initial_cash,
        "final_equity": final["equity"],
        "net_return": final["equity"] / initial_cash - 1,
        "annualized_return": (final["equity"] / initial_cash) ** (252 / len(days)) - 1,
        "max_drawdown": min(r["drawdown"] for r in curve),
        "sharpe_252": float(returns.mean() / returns.std() * np.sqrt(252))
        if returns.std()
        else None,
        "signals": len(seen),
        "entries": fills,
        "closed_trades": len(trades),
        "open_positions": len(positions),
        "win_rate": float((pnl > 0).mean()) if len(pnl) else None,
        "mean_trade_return": float(per_trade.mean()) if len(pnl) else None,
        "mean_trade_pnl": float(pnl.mean()) if len(pnl) else None,
        "mean_win_return": float(per_trade[per_trade > 0].mean())
        if (per_trade > 0).any()
        else None,
        "mean_loss_return": float(per_trade[per_trade < 0].mean())
        if (per_trade < 0).any()
        else None,
        "profit_factor": float(pnl[pnl > 0].sum() / -pnl[pnl < 0].sum())
        if (pnl < 0).any()
        else None,
        "average_holding_days": float(np.mean([r["holding_days"] for r in trades]))
        if trades
        else None,
        "average_exposure": float(np.mean([r["exposure"] for r in curve])),
        "turnover_over_initial_cash": turnover / initial_cash,
        "total_fees": total_fees,
        "entry_status_counts": dict(Counter(r["status"] for r in orders)),
        "delayed_exit_position_days": delayed_days,
        "stale_position_days": stale_days,
        "synthetic_factor_applications": factor_applications,
        "terminal_stale_value": final["stale_value"],
        "terminal_stale_zero_stress_return": (final["equity"] - final["stale_value"]) / initial_cash
        - 1,
    }
    return {
        "stats": stats,
        "equity": curve,
        "orders": orders,
        "trades": trades,
        "open_positions": list(positions.values()),
    }
