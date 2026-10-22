from datetime import date, datetime

import pytest

from app.market_time import CN_TZ
from app.services.first_board_portfolio import (
    buy_budget,
    exit_candidates,
    paper_order_block_reason,
    portfolio_snapshot,
)
from app.strategy import paper

SYMBOL = "600000.SH"
DAY = date(2026, 10, 15)


def test_budget_accounts_for_fees_and_pending_cash(tmp_path):
    paper.create_account(tmp_path, 100_000, commission_pct=.001, slippage_bps=10)
    prices, sectors = {SYMBOL: 10.0}, {SYMBOL: "金融-银行"}
    before = buy_budget(tmp_path, "default", symbol=SYMBOL, prices=prices, sectors=sectors, config={})
    assert before["allowed"] and before["estimated_cost"] <= 25_000
    assert before["estimated_cost"] > before["amount"]
    order, error = paper.create_order(tmp_path, SYMBOL, "buy", qty=1000, ref_price=10.0)
    assert error is None and order["status"] == "pending"
    after = buy_budget(tmp_path, "default", symbol=SYMBOL, prices=prices, sectors=sectors, config={})
    assert after["qty"] < before["qty"]
    assert after["reserved_cash"] > 10_000


@pytest.mark.parametrize("symbol,cash,expected", [
    ("688001.SH", 6000, False), ("688001.SH", 100_000, True),
    ("689009.SH", 100_000, False),
])
def test_science_board_minimum_quantity_and_receipt_guard(tmp_path, symbol, cash, expected):
    paper.create_account(tmp_path, cash)
    result = buy_budget(tmp_path, "default", symbol=symbol, prices={symbol: 10.0},
                        sectors={symbol: "电子-芯片"}, config={})
    assert result["allowed"] is expected
    if expected:
        assert result["qty"] >= 200 and result["qty"] % 100 == 0
    if symbol.startswith("689"):
        assert paper_order_block_reason(symbol)


def test_missing_holdings_price_is_not_valued_at_cost(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    paper.create_account(tmp_path, 100_000)
    paper.create_order(tmp_path, SYMBOL, "buy", qty=100, ref_price=10.0)
    assert len(paper.evaluate_intraday(tmp_path, {SYMBOL: 10.0})) == 1
    snapshot = portfolio_snapshot(tmp_path, "default", {})
    assert snapshot["initialized"]
    assert snapshot["holdings"][0]["market_value"] is None
    assert snapshot["total"] is None
    result = buy_budget(tmp_path, "default", symbol="600001.SH", prices={"600001.SH": 10.0},
                        sectors={SYMBOL: "金融-银行", "600001.SH": "金融-银行"}, config={})
    assert not result["allowed"] and result["missing_prices"] == [SYMBOL]


def test_exit_only_for_linked_lots_after_t1_and_pending_sells(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    paper.create_account(tmp_path, 100_000)
    paper.create_order(tmp_path, SYMBOL, "buy", qty=200, ref_price=10.0, source="first_board:signal123")
    assert len(paper.evaluate_intraday(tmp_path, {SYMBOL: 10.0})) == 1
    same_day = datetime(2026, 10, 15, 14, 0, tzinfo=CN_TZ)
    assert exit_candidates(tmp_path, "default", {SYMBOL: 9.0}, now=same_day) == []
    tomorrow = datetime(2026, 10, 16, 10, 31, tzinfo=CN_TZ)
    monkeypatch.setattr(paper, "cn_today", lambda: tomorrow.date())
    result = exit_candidates(tmp_path, "default", {SYMBOL: 10.0}, now=tomorrow)
    assert result[0]["qty"] == 200
    _, error = paper.create_order(tmp_path, SYMBOL, "sell", qty=100)
    assert error is None
    result = exit_candidates(tmp_path, "default", {SYMBOL: 10.0}, now=tomorrow)
    assert result[0]["qty"] == 100


def test_unlinked_manual_purchase_has_no_first_board_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    paper.create_account(tmp_path, 100_000)
    paper.create_order(tmp_path, SYMBOL, "buy", qty=100, ref_price=10.0, source="manual")
    paper.evaluate_intraday(tmp_path, {SYMBOL: 10.0})
    assert exit_candidates(tmp_path, "default", {SYMBOL: 9.0},
                           now=datetime(2026, 10, 16, 14, 0, tzinfo=CN_TZ)) == []


def _fill_order(tmp_path, side, qty, source="manual", price=10.0):
    order, error = paper.create_order(tmp_path, SYMBOL, side, qty=qty,
                                      ref_price=price, source=source)
    assert error is None
    assert len(paper.evaluate_intraday(tmp_path, {SYMBOL: price})) == 1
    assert paper.get_order(tmp_path, order["id"])["status"] == "filled"


@pytest.mark.parametrize("manual_first", [False, True])
@pytest.mark.parametrize("sold_qty", [0, 100, 200, 300])
def test_same_day_mixed_sources_have_no_attributable_exit(
    tmp_path, monkeypatch, manual_first, sold_qty,
):
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    paper.create_account(tmp_path, 100_000)
    buys = [(200, "first_board:signal123"), (300, "manual")]
    for qty, source in reversed(buys) if manual_first else buys:
        _fill_order(tmp_path, "buy", qty, source)
    tomorrow = datetime(2026, 10, 16, 10, 31, tzinfo=CN_TZ)
    monkeypatch.setattr(paper, "cn_today", lambda: tomorrow.date())
    if sold_qty:
        _fill_order(tmp_path, "sell", sold_qty)
    # 日期相同的剩余 lots 没有订单来源; 部分卖出后也不能猜剩余属于首板。
    assert exit_candidates(tmp_path, "default", {SYMBOL: 9.0}, now=tomorrow) == []


def test_only_first_board_lots_keep_exit_after_partial_fill(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    paper.create_account(tmp_path, 100_000)
    _fill_order(tmp_path, "buy", 200, "first_board:first")
    _fill_order(tmp_path, "buy", 300, "first_board:second")
    tomorrow = datetime(2026, 10, 16, 10, 31, tzinfo=CN_TZ)
    monkeypatch.setattr(paper, "cn_today", lambda: tomorrow.date())
    _fill_order(tmp_path, "sell", 300)
    candidates = exit_candidates(tmp_path, "default", {SYMBOL: 10.0}, now=tomorrow)
    assert len(candidates) == 1 and candidates[0]["qty"] == 200


@pytest.mark.parametrize("sold_qty,expected_qty", [(0, 0), (100, 0), (200, 300)])
def test_exit_does_not_cross_older_manual_fifo_lots(
    tmp_path, monkeypatch, sold_qty, expected_qty,
):
    monkeypatch.setattr(paper, "cn_today", lambda: date(2026, 10, 14))
    paper.create_account(tmp_path, 100_000)
    _fill_order(tmp_path, "buy", 200)
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    _fill_order(tmp_path, "buy", 300, "first_board:signal123")
    tomorrow = datetime(2026, 10, 16, 10, 31, tzinfo=CN_TZ)
    monkeypatch.setattr(paper, "cn_today", lambda: tomorrow.date())
    if sold_qty:
        _fill_order(tmp_path, "sell", sold_qty)
    candidates = exit_candidates(tmp_path, "default", {SYMBOL: 10.0}, now=tomorrow)
    assert sum(item["qty"] for item in candidates) == expected_qty


@pytest.mark.parametrize("pending_qty,expected_qty", [(0, 200), (100, 100), (200, 0), (300, 0)])
def test_exit_stops_before_later_mixed_source_fifo_lots(
    tmp_path, monkeypatch, pending_qty, expected_qty,
):
    monkeypatch.setattr(paper, "cn_today", lambda: date(2026, 10, 14))
    paper.create_account(tmp_path, 100_000)
    _fill_order(tmp_path, "buy", 200, "first_board:earlier")
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    _fill_order(tmp_path, "buy", 200, "first_board:later")
    _fill_order(tmp_path, "buy", 300)
    tomorrow = datetime(2026, 10, 16, 10, 31, tzinfo=CN_TZ)
    monkeypatch.setattr(paper, "cn_today", lambda: tomorrow.date())
    if pending_qty:
        _, error = paper.create_order(tmp_path, SYMBOL, "sell", qty=pending_qty)
        assert error is None
    candidates = exit_candidates(tmp_path, "default", {SYMBOL: 10.0}, now=tomorrow)
    assert sum(item["qty"] for item in candidates) == expected_qty


@pytest.mark.parametrize("manual_price,current_price,expected_qty", [
    (12.0, 10.5, 0),  # 后续高价手工仓不能把盈利的首板批次变成止损。
    (8.0, 9.5, 200),  # 后续低价手工仓也不能掩盖首板批次的亏损。
])
def test_stop_loss_uses_first_board_cost_not_mixed_account_average(
    tmp_path, monkeypatch, manual_price, current_price, expected_qty,
):
    paper.create_account(tmp_path, 100_000, slippage_bps=0)
    monkeypatch.setattr(paper, "cn_today", lambda: date(2026, 10, 14))
    _fill_order(tmp_path, "buy", 200, "first_board:entry", price=10.0)
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    _fill_order(tmp_path, "buy", 200, price=manual_price)
    now = datetime(2026, 10, 16, 9, 45, tzinfo=CN_TZ)
    candidates = exit_candidates(tmp_path, "default", {SYMBOL: current_price}, now=now)
    assert sum(item["qty"] for item in candidates) == expected_qty
    if candidates:
        assert "止损" in candidates[0]["reason"]


@pytest.mark.parametrize("sold_qty,pending_qty,price,expected_qty", [
    (100, 0, 10.8, 300),
    (100, 100, 11.0, 200),
    (0, 200, 11.0, 200),
    (100, 300, 9.0, 0),
])
def test_stop_loss_cost_excludes_sold_and_reserved_fifo_batches(
    tmp_path, monkeypatch, sold_qty, pending_qty, price, expected_qty,
):
    paper.create_account(tmp_path, 100_000, slippage_bps=0)
    monkeypatch.setattr(paper, "cn_today", lambda: date(2026, 10, 14))
    _fill_order(tmp_path, "buy", 200, "first_board:earlier", price=10.0)
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    _fill_order(tmp_path, "buy", 200, "first_board:later", price=12.0)
    now = datetime(2026, 10, 16, 9, 45, tzinfo=CN_TZ)
    monkeypatch.setattr(paper, "cn_today", lambda: now.date())
    if sold_qty:
        _fill_order(tmp_path, "sell", sold_qty)
    if pending_qty:
        _, error = paper.create_order(tmp_path, SYMBOL, "sell", qty=pending_qty)
        assert error is None
    candidates = exit_candidates(tmp_path, "default", {SYMBOL: price}, now=now)
    assert sum(item["qty"] for item in candidates) == expected_qty


@pytest.mark.parametrize("field,value", [
    ("price", None), ("price", float("nan")), ("fee", -1), ("fee", float("inf")),
    ("qty", 100), ("date", "not-a-date"), ("kind", "unknown"), ("order_id", None),
])
def test_unknown_batch_cost_or_source_fails_closed_even_at_exit_time(
    tmp_path, monkeypatch, field, value,
):
    paper.create_account(tmp_path, 100_000)
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    _fill_order(tmp_path, "buy", 200, "first_board:entry")
    fills = paper.load_fills(tmp_path)
    fills[0][field] = value
    monkeypatch.setattr(paper, "load_fills", lambda *args: fills)
    assert exit_candidates(tmp_path, "default", {SYMBOL: 9.0},
                           now=datetime(2026, 10, 16, 10, 31, tzinfo=CN_TZ)) == []


def test_legacy_fills_without_kind_or_fee_use_existing_paper_contract(tmp_path, monkeypatch):
    paper.create_account(tmp_path, 100_000, slippage_bps=0)
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    _fill_order(tmp_path, "buy", 200, "first_board:entry")
    fills = paper.load_fills(tmp_path)
    for key in ("kind", "fee", "seq", "ts"):
        fills[0].pop(key)
    positions, _ = paper.replay_positions(tmp_path, fills=fills)
    monkeypatch.setattr(paper, "load_fills", lambda *args: fills)
    monkeypatch.setattr(paper, "load_positions", lambda *args: positions)
    now = datetime(2026, 10, 16, 9, 45, tzinfo=CN_TZ)
    assert exit_candidates(tmp_path, "default", {SYMBOL: 9.71}, now=now) == []
    candidates = exit_candidates(tmp_path, "default", {SYMBOL: 9.69}, now=now)
    assert candidates[0]["qty"] == 200


def test_batch_cost_tracks_corporate_action_without_changing_purchase_day(tmp_path, monkeypatch):
    paper.create_account(tmp_path, 100_000, slippage_bps=0)
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    _fill_order(tmp_path, "buy", 200, "first_board:entry")
    fills = paper.load_fills(tmp_path)
    fills.append({"kind": "corp_action", "symbol": SYMBOL, "date": "2026-10-16", "factor": 2.0})
    positions, _ = paper.replay_positions(tmp_path, fills=fills)
    monkeypatch.setattr(paper, "load_fills", lambda *args: fills)
    monkeypatch.setattr(paper, "load_positions", lambda *args: positions)
    now = datetime(2026, 10, 16, 9, 45, tzinfo=CN_TZ)
    assert exit_candidates(tmp_path, "default", {SYMBOL: 5.0}, now=now) == []
    candidates = exit_candidates(tmp_path, "default", {SYMBOL: 4.8}, now=now)
    assert candidates[0]["qty"] == 400


def test_batch_stop_loss_includes_actual_buy_fee(tmp_path, monkeypatch):
    paper.create_account(tmp_path, 100_000, slippage_bps=0, commission_pct=0)
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    _fill_order(tmp_path, "buy", 200, "first_board:entry")
    # 200 股 * 10 元 + 最低佣金 5 元, 单位成本 10.025, 3% 阈值 9.72425。
    now = datetime(2026, 10, 16, 9, 45, tzinfo=CN_TZ)
    assert exit_candidates(tmp_path, "default", {SYMBOL: 9.73}, now=now) == []
    assert exit_candidates(tmp_path, "default", {SYMBOL: 9.72}, now=now)[0]["qty"] == 200


def test_buy_order_symbol_must_match_fill_for_attribution(tmp_path, monkeypatch):
    paper.create_account(tmp_path, 100_000)
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    _fill_order(tmp_path, "buy", 200, "first_board:entry")
    orders = paper.load_orders(tmp_path)
    orders[0]["symbol"] = "600001.SH"
    monkeypatch.setattr(paper, "load_orders", lambda *args: orders)
    assert exit_candidates(tmp_path, "default", {SYMBOL: 9.0},
                           now=datetime(2026, 10, 16, 10, 31, tzinfo=CN_TZ)) == []


@pytest.mark.parametrize("factor", [None, 0, -1, float("inf")])
def test_invalid_corporate_action_cost_fails_closed(tmp_path, monkeypatch, factor):
    paper.create_account(tmp_path, 100_000)
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    _fill_order(tmp_path, "buy", 200, "first_board:entry")
    fills = paper.load_fills(tmp_path)
    fills.append({"kind": "corp_action", "symbol": SYMBOL, "date": "2026-10-16", "factor": factor})
    monkeypatch.setattr(paper, "load_fills", lambda *args: fills)
    assert exit_candidates(tmp_path, "default", {SYMBOL: 9.0},
                           now=datetime(2026, 10, 16, 10, 31, tzinfo=CN_TZ)) == []


@pytest.mark.parametrize("factor,total_qty,lot_qty", [
    (1.000000004, 2000.000008, 2000.0),
    (1.013654321, 2027.308642, 2027.30864),
])
@pytest.mark.parametrize("sold_qty", [0, 100])
def test_corporate_action_total_and_lots_keep_paper_rounding_contract(
    tmp_path, monkeypatch, factor, total_qty, lot_qty, sold_qty,
):
    paper.create_account(tmp_path, 100_000, slippage_bps=0)
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    for index in range(20):
        _fill_order(tmp_path, "buy", 100, f"first_board:entry{index}")
    fills = paper.load_fills(tmp_path)
    fills.append({"kind": "corp_action", "symbol": SYMBOL, "date": "2026-10-16",
                  "factor": factor})
    if sold_qty:
        fills.append({"symbol": SYMBOL, "side": "sell", "date": "2026-10-16",
                      "qty": sold_qty, "price": 10.0, "fee": 5.0})
    positions, _ = paper.replay_positions(tmp_path, fills=fills)
    assert positions[SYMBOL]["qty"] == pytest.approx(total_qty - sold_qty, rel=0, abs=1e-9)
    assert sum(lot["qty"] for lot in positions[SYMBOL]["lots"]) == pytest.approx(
        lot_qty - sold_qty, rel=0, abs=1e-9)
    monkeypatch.setattr(paper, "load_fills", lambda *args: fills)
    monkeypatch.setattr(paper, "load_positions", lambda *args: positions)
    now = datetime(2026, 10, 16, 9, 45, tzinfo=CN_TZ)
    candidates = exit_candidates(tmp_path, "default", {SYMBOL: 9.0}, now=now)
    available = paper._available_of(positions[SYMBOL], now.date().isoformat())
    assert candidates[0]["qty"] == available == int(lot_qty - sold_qty)
    time_exits = exit_candidates(tmp_path, "default", {SYMBOL: 10.0}, now=now.replace(hour=10, minute=31))
    assert time_exits[0]["qty"] == available
