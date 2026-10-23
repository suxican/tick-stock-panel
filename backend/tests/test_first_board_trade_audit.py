import json
from datetime import date, datetime

import pytest

from app.market_time import CN_TZ
from app.services import first_board_ledger as ledger
from app.services import first_board_trade_audit as audit
from app.services.first_board_trade_audit import build_paper_trade_audit

SYMBOL = "600000.SH"
START = date(2026, 10, 20)
END = date(2026, 10, 22)


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(audit, "cn_today", lambda: END)


def _stamp(day=20, hour=9, minute=40):
    return datetime(2026, 10, day, hour, minute, tzinfo=CN_TZ).isoformat()


def _signal(identifier="signal_a", revision=1, day=20):
    return {"id": identifier, "symbol": SYMBOL, "event_type": "buy_candidate",
            "date": f"2026-10-{day}", "ts": int(datetime.fromisoformat(_stamp(day)).timestamp() * 1000),
            "revision": revision, "pattern": "platform", "evidence": {}}


def _order(identifier, side="buy", qty=100, source="first_board:signal_a", status="filled", day=20):
    return {"id": identifier, "symbol": SYMBOL, "side": side, "qty": qty, "source": source,
            "status": status, "created_at": _stamp(day, minute=41),
            "filled_at": _stamp(day, minute=42) if status == "filled" else None,
            "ref_price": 10., "reason": None, "asset_type": "stock"}


def _fill(order, price=10., fee=5.):
    return {"kind": "fill", "order_id": order["id"], "symbol": order["symbol"],
            "date": order["created_at"][:10], "ts": order["filled_at"], "side": order["side"],
            "qty": order["qty"], "price": price, "fee": fee, "asset_type": "stock"}


def _write(tmp_path, orders=(), fills=(), *, legacy=False):
    root = tmp_path / "paper" if legacy else tmp_path / "paper" / "accounts" / "default"
    root.mkdir(parents=True)
    (root / "account.json").write_text(json.dumps({"id": "default", "initial_cash": 100000,
                                                   "cash": 100000, "status": "active"}), encoding="utf-8")
    (root / "orders").mkdir()
    for order in orders:
        (root / "orders" / f"order_{order['id']}.json").write_text(json.dumps(order), encoding="utf-8")
    (root / "fills.jsonl").write_text("\n".join(json.dumps(f) for f in fills), encoding="utf-8")
    return root


def _audit(tmp_path, events=None, **kwargs):
    return build_paper_trade_audit(tmp_path, "default", events=events or [_signal()],
                                  start=START, end=END, **kwargs)


@pytest.mark.parametrize("state,expected", [(None, "not_ordered"), ("pending", "pending"),
                                           ("cancelled", "cancelled"), ("expired", "expired")])
def test_unfilled_states_keep_null_returns_and_full_signal_denominator(tmp_path, state, expected):
    orders = [_order("buy", status=state)] if state else []
    _write(tmp_path, orders)
    result = _audit(tmp_path)
    assert result["status"] == "ready"
    assert result["coverage"]["signal_count"] == 1
    row = result["trades"][0]
    assert row["status"] == expected
    assert row["realized_net_pnl"] is None and row["closed_net_return"] is None
    assert result["summary"]["closed_win_rate"] is None


def test_full_account_fifo_attributes_manual_sell_only_to_consumed_first_board_lots(tmp_path):
    manual = _order("manual", source="manual")
    buy = _order("buy")
    sell = _order("sell", "sell", 150, "manual", day=21)
    _write(tmp_path, [manual, buy, sell], [_fill(manual, 8), _fill(buy), _fill(sell, 12, 6)])
    result = _audit(tmp_path)
    row = result["trades"][0]
    assert row["status"] == "partially_closed" and row["remaining_qty"] == 50
    assert row["realized_net_pnl"] == pytest.approx(95.5)
    assert row["closed_net_return"] is None
    assert row["exits"][0]["manual_exit"] is True
    assert row["exits"][0]["sell_order_id"] == "sell"
    assert result["summary"]["closed_signals"] == 0
    assert result["summary"]["closed_win_rate"] is None


def test_partial_sales_are_one_closed_signal_and_do_not_double_charge_slippage(tmp_path):
    buy = _order("buy", qty=200)
    sell1 = _order("sell1", "sell", 100, "first_board:exit_a", day=21)
    sell2 = _order("sell2", "sell", 100, "manual", day=22)
    _write(tmp_path, [buy, sell1, sell2], [_fill(buy), _fill(sell1, 12), _fill(sell2, 8)])
    result = _audit(tmp_path)
    row = result["trades"][0]
    assert row["status"] == "closed" and row["remaining_qty"] == 0
    assert row["realized_net_pnl"] == -15
    assert row["closed_net_return"] == pytest.approx(-15 / 2005)
    assert len(row["exits"]) == 2
    assert result["summary"]["closed_signals"] == 1
    assert result["summary"]["closed_win_rate"] == 0


def test_pending_exit_does_not_realize_pnl_or_reduce_open_quantity(tmp_path):
    buy = _order("buy", qty=200)
    sell = _order("sell", "sell", 100, "manual", status="pending", day=21)
    _write(tmp_path, [buy, sell], [_fill(buy)])
    row = _audit(tmp_path)["trades"][0]
    assert row["status"] == "open" and row["remaining_qty"] == 200
    assert row["realized_net_pnl"] == 0 and row["exits"] == []


def test_corporate_action_preserves_cost_and_adjusts_closed_quantity(tmp_path):
    buy = _order("buy")
    sell = _order("sell", "sell", 200, "manual", day=22)
    action = {"kind": "corp_action", "symbol": SYMBOL, "date": "2026-10-21", "factor": 2.}
    _write(tmp_path, [buy, sell], [_fill(buy), action, _fill(sell, 6)])
    row = _audit(tmp_path)["trades"][0]
    assert row["status"] == "closed" and row["remaining_qty"] == 0
    assert row["realized_net_pnl"] == 190
    assert row["closed_net_return"] == pytest.approx(190 / 1005)


def test_corporate_action_after_partial_sale_preserves_only_remaining_cost(tmp_path):
    buy = _order("buy", qty=200)
    first = _order("sell1", "sell", 100, "manual", day=21)
    second = _order("sell2", "sell", 200, "manual", day=22)
    action = {"kind": "corp_action", "symbol": SYMBOL, "date": "2026-10-21", "factor": 2., "qty_before": 100}
    _write(tmp_path, [buy, first, second], [_fill(buy), _fill(first, 12), action, _fill(second, 6)])
    row = _audit(tmp_path)["trades"][0]
    assert row["status"] == "closed" and row["realized_net_pnl"] == 385
    assert row["closed_net_return"] == pytest.approx(385 / 2005)


@pytest.mark.parametrize("problem", ["same_day_sale", "before_signal", "mismatched_symbol",
                                    "overfill", "no_timestamp", "duplicate_order_fill"])
def test_invalid_trade_evidence_never_produces_verified_net_returns(tmp_path, problem):
    buy = _order("buy")
    fills = [_fill(buy)]
    orders = [buy]
    if problem == "same_day_sale":
        sell = _order("sell", "sell", 100, "manual")
        orders.append(sell)
        fills.append(_fill(sell, 11))
    elif problem == "before_signal":
        buy["created_at"] = _stamp(minute=39)
    elif problem == "mismatched_symbol":
        fills[0]["symbol"] = "600001.SH"
    elif problem == "overfill":
        fills[0]["qty"] = 200
    elif problem == "no_timestamp":
        fills[0]["ts"] = None
    else:
        fills.append(dict(fills[0]))
    _write(tmp_path, orders, fills)
    result = _audit(tmp_path)
    assert result["status"] in {"error", "incomplete"}
    assert result["trades"][0]["status"] == "evidence_missing"
    assert result["summary"]["realized_net_pnl"] is None


def test_legacy_layout_and_missing_fee_are_read_only_and_explicit(tmp_path):
    buy = _order("buy")
    fill = _fill(buy)
    fill.pop("fee")
    fill.pop("kind")
    root = _write(tmp_path, [buy], [fill], legacy=True)
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = _audit(tmp_path)
    after = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert before == after and not (root / "accounts").exists()
    assert result["status"] == "incomplete" and result["coverage"]["legacy_fee_default_count"] == 1
    assert result["trades"][0]["fills"][0]["fee_defaulted"] is True
    assert result["trades"][0]["status"] == "evidence_missing"
    assert result["summary"]["realized_net_pnl"] is None


@pytest.mark.parametrize("file_name", ["account.json", "fills.jsonl", "orders/order_bad.json"])
def test_corrupt_record_is_not_silently_skipped(tmp_path, file_name):
    root = _write(tmp_path)
    (root / file_name).write_text("{broken", encoding="utf-8")
    result = _audit(tmp_path)
    assert result["status"] == "error"
    assert result["summary"]["closed_win_rate"] is None
    assert result["trades"][0]["status"] == "evidence_missing"


def test_missing_account_does_not_create_or_migrate_paths(tmp_path):
    result = _audit(tmp_path)
    assert result["status"] == "unavailable" and list(tmp_path.iterdir()) == []
    result = build_paper_trade_audit(tmp_path, None, events=[_signal()], start=START, end=END)
    assert result["status"] == "unavailable" and list(tmp_path.iterdir()) == []


def test_detail_limit_does_not_truncate_summary_or_revision_filter(tmp_path):
    _write(tmp_path)
    signals = [_signal(f"signal_{i}") for i in range(501)] + [_signal("other", revision=2)]
    result = _audit(tmp_path, signals, revision=1)
    assert len(result["trades"]) == 500 and result["coverage"]["truncated"]
    assert result["summary"]["unsubmitted_signals"] == result["coverage"]["signal_count"] == 501
    assert result["fingerprint"] == _audit(tmp_path, signals, revision=1)["fingerprint"]


def test_ledger_size_limit_is_explicit(tmp_path, monkeypatch):
    _write(tmp_path, [_order("a", source="manual"), _order("b", source="manual")])
    monkeypatch.setattr(ledger, "MAX_ORDERS", 1)
    result = _audit(tmp_path)
    assert result["status"] == "error" and "上限" in result["reason"]


def test_materialized_quantity_mismatch_is_not_certified(tmp_path):
    buy = _order("buy")
    root = _write(tmp_path, [buy], [_fill(buy)])
    (root / "positions.json").write_text(json.dumps({"positions": {
        SYMBOL: {"qty": 200, "lots": [{"date": "2026-10-20", "qty": 200}]},
    }}), encoding="utf-8")
    result = _audit(tmp_path)
    assert result["status"] == "error" and result["summary"]["realized_net_pnl"] is None


def test_initial_revision_zero_remains_supported(tmp_path):
    buy = _order("buy")
    _write(tmp_path, [buy], [_fill(buy)])
    result = _audit(tmp_path, [_signal(revision=0)], revision=0)
    assert result["status"] == "ready" and result["trades"][0]["status"] == "open"


def test_historical_cutoff_excludes_later_exit_and_corporate_action(tmp_path):
    buy = _order("buy")
    sell = _order("sell", "sell", 200, "manual", day=22)
    action = {"kind": "corp_action", "symbol": SYMBOL, "date": "2026-10-21", "factor": 2.}
    _write(tmp_path, [buy, sell], [_fill(buy), action, _fill(sell, 6)])
    result = build_paper_trade_audit(tmp_path, "default", events=[_signal()], start=START, end=START)
    row = result["trades"][0]
    assert row["status"] == "open" and row["remaining_qty"] == 100
    assert row["exits"] == [] and row["realized_net_pnl"] == 0
    assert result["summary"]["closed_win_rate"] is None


def test_historical_future_fill_restores_pending_and_future_order_is_absent(tmp_path):
    buy = _order("buy")
    buy["filled_at"] = _stamp(day=21)
    fill = {**_fill(buy), "date": "2026-10-21"}
    later = _order("later", source="first_board:signal_b", day=21)
    _write(tmp_path, [buy, later], [fill, _fill(later)])
    result = build_paper_trade_audit(tmp_path, "default", events=[_signal(), _signal("signal_b")],
                                   start=START, end=START)
    rows = {row["signal_id"]: row for row in result["trades"]}
    assert rows["signal_a"]["status"] == "pending" and rows["signal_a"]["fills"] == []
    assert rows["signal_b"]["status"] == "not_ordered"
    assert result["summary"]["realized_net_pnl"] is None


@pytest.mark.parametrize("state", ["cancelled", "expired"])
def test_historical_termination_without_timestamp_is_unknown(tmp_path, state):
    _write(tmp_path, [_order("buy", status=state)])
    result = build_paper_trade_audit(tmp_path, "default", events=[_signal()], start=START, end=START)
    assert result["status"] == "incomplete"
    assert result["trades"][0]["status"] == "evidence_missing"
    assert result["coverage"]["attributable_count"] == 0
    assert result["summary"]["realized_net_pnl"] is None


@pytest.mark.parametrize("missing", ["buy", "sell", None])
def test_missing_fee_is_excluded_but_explicit_zero_is_valid(tmp_path, missing):
    buy = _order("buy")
    sell = _order("sell", "sell", 100, "manual", day=21)
    fills = [_fill(buy, fee=0), _fill(sell, 11, fee=0)]
    if missing is not None:
        fills[0 if missing == "buy" else 1].pop("fee")
    _write(tmp_path, [buy, sell], fills)
    result = _audit(tmp_path)
    if missing is None:
        assert result["trades"][0]["status"] == "closed"
        assert result["summary"]["closed_win_rate"] == 1
        assert result["summary"]["realized_net_pnl"] == 100
    else:
        assert result["trades"][0]["status"] == "evidence_missing"
        assert result["summary"]["closed_signals"] == 0
        assert result["summary"]["closed_win_rate"] is None
        assert result["summary"]["realized_net_pnl"] is None
        assert result["trades"][0]["exits"][0]["net_pnl"] is None


def test_unrelated_manual_fee_gap_does_not_disqualify_verified_first_board_slice(tmp_path):
    manual = _order("manual", source="manual")
    buy = _order("buy")
    sell = _order("sell", "sell", 200, "manual", day=21)
    manual_fill = _fill(manual)
    manual_fill.pop("fee")
    _write(tmp_path, [manual, buy, sell], [manual_fill, _fill(buy), _fill(sell, 11, 10)])
    result = _audit(tmp_path)
    assert result["status"] == "ready"
    assert result["summary"]["realized_net_pnl"] == 90
    assert result["coverage"]["legacy_fee_default_count"] == 1


def test_sell_source_is_not_claimed_as_verified_strategy_exit_without_its_event(tmp_path):
    buy = _order("buy")
    sell = _order("sell", "sell", 100, "first_board:exit_a", day=21)
    _write(tmp_path, [buy, sell], [_fill(buy), _fill(sell, 11)])
    exit_row = _audit(tmp_path)["trades"][0]["exits"][0]
    assert exit_row["exit_signal_id"] == "exit_a"
    assert exit_row["exit_signal_verified"] is False and exit_row["manual_exit"] is False


def test_verified_exit_requires_matching_symbol_event_and_time(tmp_path):
    buy = _order("buy")
    sell = _order("sell", "sell", 100, "first_board:exit_a", day=21)
    _write(tmp_path, [buy, sell], [_fill(buy), _fill(sell, 11)])
    exit_event = {**_signal("exit_a", day=21), "event_type": "exit_candidate"}
    assert _audit(tmp_path, [_signal(), exit_event])["trades"][0]["exits"][0]["exit_signal_verified"] is True


def test_corrupt_source_before_signal_does_not_make_negative_coverage(tmp_path):
    buy = _order("buy", status="pending")
    buy["created_at"] = _stamp(minute=39)
    _write(tmp_path, [buy])
    result = _audit(tmp_path)
    assert result["coverage"]["attributable_count"] == 0
    assert result["coverage"]["unknown_count"] == 1


@pytest.mark.parametrize("problem", ["missing_source", "missing_order", "record_time_reversal", "future_date",
                                    "invalid_action", "bad_json_number", "duplicate_order_id",
                                    "duplicate_action", "bad_action_quantity", "empty_account"])
def test_entire_ledger_inconsistency_fails_closed(tmp_path, problem):
    buy = _order("buy")
    sell = _order("sell", "sell", 100, "manual", day=21)
    orders, fills = [buy, sell], [_fill(buy), _fill(sell, 11)]
    if problem == "missing_source":
        buy.pop("source")
    elif problem == "missing_order":
        orders.pop()
    elif problem == "record_time_reversal":
        fills[0]["ts"] = _stamp(day=22)
    elif problem == "future_date":
        fills[1]["date"] = "2026-10-22"
    elif problem == "invalid_action":
        fills.insert(1, {"kind": "corp_action", "symbol": SYMBOL, "date": "2026-10-21", "factor": 0})
    elif problem == "bad_json_number":
        fills[0]["fee"] = float("nan")
    elif problem in {"duplicate_action", "bad_action_quantity"}:
        action = {"kind": "corp_action", "symbol": SYMBOL, "date": "2026-10-21", "factor": 1.}
        if problem == "bad_action_quantity":
            action["qty_before"] = 999
        fills.insert(1, action)
        if problem == "duplicate_action":
            fills.insert(1, dict(action))
    root = _write(tmp_path, orders, fills)
    if problem == "duplicate_order_id":
        (root / "orders" / "order_copy.json").write_text(json.dumps(buy), encoding="utf-8")
    elif problem == "empty_account":
        (root / "account.json").write_text("{}", encoding="utf-8")
    result = _audit(tmp_path)
    assert result["status"] == "error"
    assert result["summary"]["realized_net_pnl"] is None and result["summary"]["closed_win_rate"] is None
