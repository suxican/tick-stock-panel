from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.market_time import CN_TZ
from app.strategy.position_t import (
    DEFAULT_CONFIG,
    estimate_costs,
    evaluate_candidate,
    legal_quantity,
    order_fee,
    validate_execution,
    validate_quantity,
)


def sample_context(direction="buy_sell"):
    start = datetime(2026, 10, 16, 9, 31, tzinfo=CN_TZ)
    bars = []
    for i in range(50):
        close = 10.4 if i < 30 else 10.0
        high, low, vol = close + .04, close - .04, 10000
        if i == 38:
            low = 9.85
        if i >= 47:
            close = [9.9, 9.94, 9.98][i - 47]
            high, low = close + .02, close - .02
        if i == 49:
            vol = 12000
        if direction == "sell_buy":
            close, high, low = 20 - close, 20 - low, 20 - high
        bars.append({"datetime": start + timedelta(minutes=i), "open": close,
                     "high": high, "low": low, "close": close,
                     "volume_shares": vol, "amount": vol * close, "complete": True})
    now = bars[-1]["datetime"] + timedelta(seconds=5)
    quote = {"price": bars[-1]["close"], "bid": bars[-1]["close"] - .01,
             "ask": bars[-1]["close"] + .01, "source": "fixture",
             "market_ts": now - timedelta(seconds=2), "received_ts": now,
             "trade_status": "trading", "is_trading_day": True,
             "limit_up_price": 11, "limit_down_price": 9, "history_days": 20,
             "median_minute_volume_shares": 10000}
    env = {"market_return_5m": 0, "sector_return_5m": 0, "market_ts": now,
           "sector_ts": now, "coverage": 1}
    account = {"id": "account", "mode": "manual", "available_cash": 10000,
               "effective_cash": 10000, "trade_date": "2026-10-16",
               "snapshot_at": now, "reconciled": True, "version": 1}
    position = {"symbol": "000001.SZ", "exchange": "SZ", "board": "main",
                "security_type": "stock", "qty": 1000, "sellable_qty": 1000,
                "effective_sellable": 1000, "core_qty": 800, "target_qty": 1000,
                "max_t_qty": 200, "max_extra_qty": 200, "reconciled": True,
                "trade_date": "2026-10-16", "snapshot_at": now, "version": 1}
    config = {**DEFAULT_CONFIG, "enabled": True, "fees_confirmed": True, "participation_rate": .02}
    return bars, quote, env, position, account, now, config


@pytest.mark.parametrize("direction", ["buy_sell", "sell_buy"])
def test_candidates_use_completed_minute_and_explicit_evidence(direction):
    bars, quote, env, position, account, now, config = sample_context(direction)
    result = evaluate_candidate(bars, quote, env, position, account, now, direction, config)
    assert result["state"] == "triggered", result
    assert result["quantity"] == 200
    assert result["net_min"] >= 20
    assert result["evidence"]["vwap"] == pytest.approx(
        sum(x["amount"] for x in bars) / sum(x["volume_shares"] for x in bars))
    assert result["source_mode"] == "manual"
    assert result["probability"] is None
    future = {**bars[-1], "datetime": now + timedelta(minutes=1), "close": 1000}
    again = evaluate_candidate([*bars, future], quote, env, position, account, now, direction, config)
    assert again["evidence"] == result["evidence"]
    assert again["net_min"] == result["net_min"]


@pytest.mark.parametrize("mutation,reason", [
    (lambda b, q, e, p, a, n, c: q.update(market_ts=n-timedelta(seconds=11)), "报价过期"),
    (lambda b, q, e, p, a, n, c: q.pop("ask"), "买卖报价"),
    (lambda b, q, e, p, a, n, c: e.pop("sector_return_5m"), "板块"),
    (lambda b, q, e, p, a, n, c: p.update(sellable_qty=None, effective_sellable=None), "可卖"),
    (lambda b, q, e, p, a, n, c: p.update(snapshot_at=n-timedelta(minutes=16)), "持仓"),
    (lambda b, q, e, p, a, n, c: q.update(is_trading_day=None), "交易日历"),
    (lambda b, q, e, p, a, n, c: b.pop(10), "分钟"),
    (lambda b, q, e, p, a, n, c: q.update(history_days=0), "历史"),
])
def test_data_quality_fails_closed(mutation, reason):
    context = sample_context()
    mutation(*context)
    b, q, e, p, a, n, c = context
    result = evaluate_candidate(b, q, e, p, a, n, "buy_sell", c)
    assert result["state"] == "paused", result
    assert result["quantity"] == 0
    assert reason in " ".join(result["reasons"])


def test_zero_sellable_and_cost_failure_cannot_be_overridden():
    b, q, e, p, a, n, c = sample_context()
    p.update(sellable_qty=0, effective_sellable=0)
    result = evaluate_candidate(b, q, e, p, a, n, "buy_sell", c)
    assert result["state"] == "blocked"
    assert "可卖" in " ".join(result["reasons"])
    p.update(sellable_qty=1000, effective_sellable=1000)
    c["min_net_profit"] = 1000
    result = evaluate_candidate(b, q, e, p, a, n, "buy_sell", c)
    assert result["state"] == "blocked"
    assert "净价差" in " ".join(result["reasons"])


def test_unknown_daily_budget_does_not_crash_monitor():
    b, q, e, p, a, n, c = sample_context()
    p["day_initial_sellable"] = None
    result = evaluate_candidate(b, q, e, p, a, n, "buy_sell", c)
    assert result["state"] == "blocked"
    assert "预算基准未知" in " ".join(result["reasons"])


def test_sell_first_reserves_extra_gap_not_complete_cover_payment():
    b, q, e, p, a, n, c = sample_context("sell_buy")
    a.update(available_cash=100, effective_cash=100)
    result = evaluate_candidate(b, q, e, p, a, n, "sell_buy", c)
    assert result["state"] == "triggered", result
    assert 0 < result["required_cash"] < 100
    p["external_sell_qty"] = 200
    result = evaluate_candidate(b, q, e, p, a, n, "sell_buy", c)
    assert result["state"] == "blocked"
    assert "核心" in " ".join(result["reasons"])


def test_fees_are_decimal_order_group_totals_and_do_not_double_count():
    cfg = {"commission_rate": .0003, "min_commission": 5, "stamp_tax_rate": .0005,
           "transfer_rate": .00001, "slippage_per_share": .01}
    assert order_fee("buy", 10, 100, cfg) == Decimal("5.01")
    assert order_fee("buy", 10, 200, cfg) - order_fee("buy", 10, 100, cfg) == Decimal("0.01")
    out = estimate_costs(10, 10.2, 200, cfg)
    assert out["net"] == pytest.approx(24.94)
    assert out["slippage"] == 4
    included = estimate_costs(10, 10.2, 200, {**cfg, "commission_includes_transfer": True,
                                                        "reference_includes_slippage": True})
    assert included["slippage"] == 0
    assert included["net"] == pytest.approx(28.98)
    with pytest.raises(ValueError, match="生效"):
        order_fee("sell", 10, 100, cfg, trade_date="2023-08-27")


def test_board_quantities_and_odd_lot_sell_exception():
    p = {"board": "main", "exchange": "SH", "security_type": "stock", "trade_date": "2026-10-16", "sellable_qty": 50}
    assert legal_quantity(201, p) == 200
    assert validate_quantity(p, "buy", 50)
    assert validate_quantity(p, "sell", 50) == []
    assert validate_quantity(p, "sell", 25)
    assert validate_quantity({**p, "sellable_qty": 150}, "sell", 150) == []
    p.update(board="star", sellable_qty=1000)
    assert legal_quantity(201, p) == 201
    assert validate_quantity(p, "buy", 199)
    assert validate_quantity(p, "buy", 100001)
    p.update(board="bse", exchange="BJ")
    assert legal_quantity(101, p) == 101
    assert validate_quantity({**p, "board": "unknown"}, "buy", 100)


def test_execution_rules_reject_auction_tick_and_price_cage():
    _, q, _, p, _, n, _ = sample_context()
    assert validate_execution("buy", 200, 10, p, q, n, is_trading_day=True) == []
    assert validate_execution("buy", 200, 10.001, p, q, n, is_trading_day=True)
    assert validate_execution("buy", 200, 10.5, p, q, n, is_trading_day=True)
    assert validate_execution("buy", 200, 10, p, q, n.replace(hour=14, minute=58), is_trading_day=True)
    assert validate_execution("buy", 200, 10, p, q, n, is_trading_day=None)


def test_input_is_not_mutated_and_unfinished_bar_not_used():
    ctx = sample_context()
    before = deepcopy(ctx)
    b, q, e, p, a, n, c = ctx
    evaluate_candidate(b, q, e, p, a, n, "buy_sell", c)
    assert ctx == before
    b[-1]["complete"] = False
    result = evaluate_candidate(b, q, e, p, a, n + timedelta(seconds=12), "buy_sell", c)
    assert result["state"] == "paused"
