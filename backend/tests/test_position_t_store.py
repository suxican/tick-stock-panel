"""持仓做 T 账本：实际成交、资源占用与人工核对的业务不变量。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app.services.position_t_store import LedgerError, PositionTStore

NOW = datetime(2026, 10, 19, 10, 20, tzinfo=timezone(timedelta(hours=8)))


@pytest.fixture
def store(tmp_path):
    return PositionTStore(tmp_path)


def account(store, cash=10000):
    return store.create_account({
        "name": "人工测试", "mode": "manual", "available_cash": cash,
        "trade_date": "2026-10-19", "snapshot_at": NOW.isoformat(), "reconciled": True,
        "config": {"enabled": True, "fees_confirmed": True, "min_net_profit": 0,
                   "min_net_return": 0, "daily_quantity_ratio": 1.0},
    }, now=NOW)


def position(store, account_id, symbol="600000.SH", **extra):
    return store.upsert_position(account_id, symbol, {
        "name": "测试股票", "exchange": "SH", "board": "main", "security_type": "stock",
        "qty": 1000, "sellable_qty": 1000, "core_qty": 800, "target_qty": 1000,
        "max_t_qty": 200, "max_extra_qty": 200, "trade_date": "2026-10-19",
        "snapshot_at": NOW.isoformat(), "reconciled": True, **extra,
    }, now=NOW)


def signal(store, aid, symbol="600000.SH", direction="buy_sell", **extra):
    acc, pos = store.get_account(aid), store.get_position(aid, symbol)
    item = {
        "id": f"sig_{symbol}_{direction}", "account_id": aid, "symbol": symbol,
        "direction": direction, "state": "triggered", "quantity": 200,
        "first_price": [10, 10], "second_price": [11, 11] if direction == "buy_sell" else [9, 9],
        "stop_price": 9.8 if direction == "buy_sell" else 10.2, "cover_guard": 10.2,
        "created_at": NOW.isoformat(), "expires_at": (NOW + timedelta(seconds=60)).isoformat(),
        "strategy_version": "test_v1", "account_version": acc["version"],
        "position_version": pos["version"], "source_mode": "manual", "reasons": [], **extra,
    }
    store.save_signals([item], now=NOW)
    return item


def plan(store, aid, symbol="600000.SH", direction="buy_sell", key=None):
    sig = signal(store, aid, symbol, direction)
    return store.create_plan(sig["id"], 200, {
        "account_version": sig["account_version"], "position_version": sig["position_version"],
    }, key or f"plan-{symbol}", now=NOW)


def fill(store, p, leg, qty=200, price=None, key=None, **extra):
    return store.record_execution({
        "plan_id": p["id"], "leg": leg, "order_id": f"order-{p['id']}-{leg}",
        "quantity": qty, "price": price or (10 if leg == 1 else 11),
        "executed_at": NOW.isoformat(), "idempotency_key": key or f"fill-{p['id']}-{leg}",
        **extra,
    }, now=NOW)


def test_same_day_inventory_never_becomes_sellable(store):
    aid = account(store)["id"]
    position(store, aid, sellable_qty=0, today_bought=1000)
    with pytest.raises(LedgerError, match="可卖"):
        plan(store, aid)


def test_buy_sell_restores_total_not_sellable_and_deduplicates(store):
    aid = account(store)["id"]
    position(store, aid)
    p = plan(store, aid)
    first = fill(store, p, 1)
    assert first["plan"]["unmatched_qty"] == 200
    assert store.get_position(aid, p["symbol"])["sellable_qty"] == 1000
    fill(store, p, 1)
    assert store.get_position(aid, p["symbol"])["qty"] == 1200
    result = fill(store, p, 2)
    pos = store.get_position(aid, p["symbol"])
    assert (pos["qty"], pos["sellable_qty"]) == (1000, 800)
    assert result["plan"]["state"] == "completed"
    assert result["plan"]["matched_pnl"] > 180


def test_partial_first_leg_tracks_actual_and_stop_keeps_second_inventory(store):
    aid = account(store)["id"]
    position(store, aid)
    p = plan(store, aid)
    result = fill(store, p, 1, qty=100)
    assert result["plan"]["unmatched_qty"] == 100
    stopped = store.stop_first_leg(p["id"], now=NOW)
    assert stopped["reserved_qty"] == 100
    assert stopped["reserved_cash"] == 0
    assert stopped["state"] == "second_pending"


def test_concurrent_acceptance_cannot_spend_same_cash(store):
    aid = account(store, cash=2500)["id"]
    position(store, aid)
    position(store, aid, "600001.SH")
    signals = [signal(store, aid, symbol) for symbol in ["600000.SH", "600001.SH"]]
    def accept(sig):
        try:
            return store.create_plan(sig["id"], 200, idempotency_key=sig["id"], now=NOW)
        except LedgerError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(accept, signals))
    assert sum(result is not None for result in results) == 1
    assert store.get_account(aid)["effective_cash"] >= 0


def test_cancel_pending_and_incomplete_terminal_report_keep_resources(store):
    aid = account(store)["id"]
    position(store, aid)
    p = plan(store, aid)
    event = {"plan_id": p["id"], "leg": 1, "order_id": "external-1", "quantity": 200,
             "cumulative_filled": 0, "cancelled_quantity": 0, "occurred_at": NOW.isoformat()}
    store.record_order_event({**event, "event_id": "submit", "status": "submitted"}, now=NOW)
    store.record_order_event({**event, "event_id": "cancel-request", "status": "cancel_pending"}, now=NOW)
    stopped = store.stop_first_leg(p["id"], now=NOW)
    assert stopped["reserved_qty"] == 200
    result = store.record_order_event({**event, "event_id": "cancel-ack", "status": "cancelled",
                                       "cumulative_filled": 100, "cancelled_quantity": 100}, now=NOW)
    assert result["reserved_qty"] >= 100
    fill(store, p, 1, qty=100, order_id="external-1")
    result = store.get_plan(p["id"])
    assert result["reserved_qty"] == 100
    assert result["reserved_cash"] == 0
    assert result["unmatched_qty"] == 100


def test_expiry_does_not_delete_exposure_and_restart_preserves_it(store, tmp_path):
    aid = account(store)["id"]
    position(store, aid)
    p = plan(store, aid)
    fill(store, p, 1)
    store.expire_signals(aid, now=NOW + timedelta(hours=5))
    restored = PositionTStore(tmp_path).get_plan(p["id"])
    assert restored["unmatched_qty"] == 200
    assert restored["reserved_qty"] == 200
    assert restored["state"] != "cancelled"


def test_sell_buy_requires_extra_gap_only_and_does_not_restore_sellable(store):
    aid = account(store, cash=500)["id"]
    position(store, aid)
    p = plan(store, aid, direction="sell_buy")
    assert 0 < p["reserved_cash"] < 500
    fill(store, p, 1, price=10, proceeds_available=True)
    done = fill(store, p, 2, price=9)
    assert done["plan"]["state"] == "completed"
    pos = store.get_position(aid, p["symbol"])
    assert (pos["qty"], pos["sellable_qty"]) == (1000, 800)


def test_manual_sale_proceeds_need_explicit_available_confirmation(store):
    aid = account(store, cash=500)["id"]
    position(store, aid)
    p = plan(store, aid, direction="sell_buy")
    fill(store, p, 1, price=10)
    acc = store.get_account(aid)
    assert acc["pending_proceeds"] > 1900
    assert acc["available_cash"] == 500
    assert "回补资金待核对" in store.get_plan(p["id"])["reasons"]


def test_net_frozen_fields_are_not_subtracted_twice(store):
    aid = account(store)["id"]
    pos = position(store, aid, sellable_qty=800, broker_frozen_qty=200,
                   sellable_net_of_frozen=True)
    assert pos["effective_sellable"] == 800
    p = plan(store, aid)
    assert store.get_position(aid, p["symbol"])["effective_sellable"] == 600


def test_snapshot_requires_exact_watermark_and_no_pending_orders(store):
    aid = account(store)["id"]
    position(store, aid)
    acc = store.get_account(aid)
    with pytest.raises(LedgerError, match="水位"):
        store.update_snapshot(aid, {"available_cash": 10000, "expected_version": acc["version"],
                                     "included_event_seq": -1}, now=NOW)
    p = plan(store, aid)
    store.record_order_event({"plan_id": p["id"], "leg": 1, "order_id": "external-1",
                              "event_id": "submit", "status": "submitted", "quantity": 200,
                              "cumulative_filled": 0}, now=NOW)
    acc = store.get_account(aid)
    with pytest.raises(LedgerError, match="委托"):
        store.update_snapshot(aid, {"available_cash": 10000, "expected_version": acc["version"],
                                     "included_event_seq": acc["event_seq"]}, now=NOW)


def test_fee_minimum_applies_once_per_order_and_allocates_to_partial_matches(store):
    aid = account(store)["id"]
    position(store, aid)
    p = plan(store, aid)
    fill(store, p, 1, qty=100, key="a")
    fill(store, p, 1, qty=100, key="b")
    executions = store.plan_detail(p["id"])["executions"]
    assert sum(item["allocated_fee"] for item in executions) == pytest.approx(5.02)
    result = fill(store, p, 2, qty=100)
    assert result["plan"]["matched_qty"] == 100
    assert result["plan"]["unmatched_qty"] == 100


def test_actual_out_of_bound_execution_is_recorded_and_flags_reconciliation(store):
    aid = account(store)["id"]
    position(store, aid)
    p = plan(store, aid)
    result = fill(store, p, 1, qty=300)
    assert result["execution"]["quantity"] == 300
    assert store.get_position(aid, p["symbol"])["qty"] == 1300
    assert store.get_account(aid)["needs_reconciliation"] is True


def test_stale_or_unknown_inventory_cannot_create_plan(store):
    aid = account(store)["id"]
    position(store, aid, sellable_qty=None)
    with pytest.raises(LedgerError, match="可卖"):
        plan(store, aid)


def test_same_stock_only_one_unfinished_plan_and_idempotent_accept(store):
    aid = account(store)["id"]
    position(store, aid)
    p = plan(store, aid, key="repeat")
    assert store.create_plan(p["signal_id"], 200, idempotency_key="repeat", now=NOW)["id"] == p["id"]
    assert store.get_plan_by_request_key("repeat")["id"] == p["id"]
    assert store.get_plan_by_request_key("unknown") is None
    with pytest.raises(LedgerError, match="幂等"):
        store.create_plan("different-signal", 200, idempotency_key="repeat", now=NOW)
    with pytest.raises(LedgerError, match="未完成"):
        plan(store, aid, key="new-key")


def test_review_counts_unmatched_exposure_in_total_equity(store):
    aid = account(store)["id"]
    position(store, aid)
    p = plan(store, aid)
    fill(store, p, 1)
    result = store.review(aid, prices={"600000.SH": 9})
    assert result["unmatched_qty"] == 200
    assert result["matched_pnl"] == 0
    assert result["incremental_equity"] < -200


def test_monitor_updates_do_not_invalidate_account_or_duplicate_risk_notice(store):
    aid = account(store)["id"]
    position(store, aid)
    p = plan(store, aid)
    fill(store, p, 1)
    before = store.get_account(aid)["version"]
    for _ in range(2):
        store.update_plan_monitor(p["id"], {"risk_codes": ["data_stale"], "data_status": "paused"}, now=NOW)
    assert store.get_account(aid)["version"] == before
    notices = [n for n in store.pending_notifications() if n["payload"]["kind"] == "plan_risk"]
    assert len(notices) == 1
    store.update_plan_monitor(p["id"], {"risk_codes": []}, now=NOW.replace(hour=15))
    notices = [n for n in store.pending_notifications() if n["payload"]["kind"] == "plan_risk"]
    assert len(notices) == 2
    assert store.get_plan(p["id"])["unmatched_qty"] == 200


def test_fee_insufficient_signal_is_rejected_at_atomic_acceptance(store):
    aid = account(store)["id"]
    position(store, aid)
    sig = signal(store, aid, second_price=[10.01, 10.01])
    with pytest.raises(LedgerError, match="净价差"):
        store.create_plan(sig["id"], 200, idempotency_key="low-profit", now=NOW)
    assert store.get_account(aid)["reserved_cash"] == 0


def test_external_sell_order_blocks_core_floor(store):
    aid = account(store)["id"]
    position(store, aid, external_sell_qty=200)
    with pytest.raises(LedgerError, match="核心"):
        plan(store, aid, direction="sell_buy")


def test_pending_proceeds_reconcile_without_double_counting(store):
    aid = account(store, cash=500)["id"]
    position(store, aid)
    p = plan(store, aid, direction="sell_buy")
    fill(store, p, 1, price=10)
    acc = store.get_account(aid)
    confirmed = store.update_snapshot(aid, {
        "expected_version": acc["version"], "included_event_seq": acc["event_seq"],
        "available_cash": acc["available_cash"] + acc["pending_proceeds"], "reconciled": True,
    }, now=NOW)
    assert confirmed["pending_proceeds"] == 0
    assert confirmed["benchmark_valid"]
    assert "回补资金待核对" not in store.get_plan(p["id"])["reasons"]


def test_late_fill_preserved_even_if_next_plan_already_exists(store):
    aid = account(store)["id"]
    position(store, aid)
    first = plan(store, aid, key="first")
    store.stop_first_leg(first["id"], now=NOW)
    second = plan(store, aid, key="second")
    fill(store, first, 1, qty=100, key="late-first")
    assert store.get_plan(first["id"])["unmatched_qty"] == 100
    assert store.get_plan(second["id"])["state"] == "first_pending"
    assert store.get_account(aid)["needs_reconciliation"]


def test_transaction_rollback_does_not_leave_partial_funds_or_plan(store, monkeypatch):
    aid = account(store)["id"]
    position(store, aid)
    sig = signal(store, aid)
    before = store.get_account(aid)
    def fail(*_args, **_kwargs):
        raise RuntimeError("simulated disk failure")
    monkeypatch.setattr(store, "_event", fail)
    with pytest.raises(RuntimeError):
        store.create_plan(sig["id"], 200, idempotency_key="rollback", now=NOW)
    assert store.list_plans(aid) == []
    assert store.get_account(aid)["reserved_cash"] == before["reserved_cash"]


def test_observations_are_idempotent_and_account_scoped(store):
    aid = account(store)["id"]
    obs = {"minute_key": NOW.isoformat(), "trade_date": "2026-10-19", "bar": {"close": 10}}
    store.save_observation(aid, "600000.SH", obs)
    store.save_observation(aid, "600000.SH", {**obs, "bar": {"close": 10.1}})
    values = store.list_observations(aid, "600000.SH", "2026-10-19", "2026-10-19")
    assert len(values) == 1 and values[0]["bar"]["close"] == 10
    assert store.list_observations("another", "600000.SH", "2026-10-19", "2026-10-19") == []


def test_signals_keep_latest_per_direction_without_repeat_notifications(store):
    aid = account(store)["id"]
    position(store, aid)
    first = signal(store, aid, episode_id="same-trigger")
    for index in range(5):
        store.save_signals([{**first, "id": f"updated-{index}"}], now=NOW)
    assert len(store.list_signals(aid)) == 1
    assert store.list_signals(aid)[0]["id"] == "updated-4"
    signals = [n for n in store.pending_notifications() if n["payload"]["kind"] == "signal"]
    assert len(signals) == 1
    signal(store, aid, direction="sell_buy", episode_id="other-direction")
    assert len(store.list_signals(aid)) == 2


def test_fee_correction_is_audited_and_idempotent_without_double_cash_charge(store):
    aid = account(store)["id"]
    position(store, aid)
    p = plan(store, aid)
    first = fill(store, p, 1)
    fill(store, p, 2, proceeds_available=True)
    before = store.get_account(aid)["available_cash"]
    pnl_before = store.get_plan(p["id"])["matched_pnl"]
    payload = {"fee": 6.02, "reason": "券商费用核对", "idempotency_key": "fee-fix"}
    corrected = store.correct_execution(first["execution"]["id"], payload, now=NOW)
    store.correct_execution(first["execution"]["id"], payload, now=NOW)
    assert store.get_account(aid)["available_cash"] == pytest.approx(before - 1)
    assert corrected["plan"]["matched_pnl"] == pytest.approx(pnl_before - 1)
    assert sum(x["kind"] == "execution_fee_correction" for x in store.plan_detail(p["id"])["events"]) == 1


def test_actual_partial_fee_is_not_overwritten_by_estimated_minimum(store):
    aid = account(store)["id"]
    position(store, aid)
    p = plan(store, aid)
    fill(store, p, 1, qty=100, key="known-fee", fee=6)
    fill(store, p, 1, qty=100, key="unknown-fee")
    executions = store.plan_detail(p["id"])["executions"]
    assert executions[0]["allocated_fee"] == 6
    assert executions[0]["fee_estimated"] is False
    assert sum(x["allocated_fee"] for x in executions) >= 6


@pytest.mark.parametrize("config", [
    {"new_plan_cutoff": "99:99"}, {"minute_timestamp_semantics": "auto"},
    {"daily_quantity_ratio": 2}, {"commission_rate": float("nan")},
    {"max_daily_plans": 1.5}, {"fee_effective_from": "2026-99-99"},
    {"fee_effective_to": "2020-01-01"},
])
def test_invalid_config_is_rejected_at_store_boundary(store, config):
    aid = account(store)["id"]
    with pytest.raises(LedgerError):
        store.update_config(aid, config, now=NOW)


def test_fractional_financial_config_is_allowed(store):
    aid = account(store)["id"]
    updated = store.update_config(aid, {"other_fee_rate": .0001, "single_risk_budget": 100.5}, now=NOW)
    assert updated["config"]["other_fee_rate"] == .0001
    assert updated["config"]["single_risk_budget"] == 100.5


def test_rejected_order_releases_after_plan_abandoned(store):
    aid = account(store)["id"]
    position(store, aid)
    p = plan(store, aid)
    store.record_order_event({"plan_id": p["id"], "leg": 1, "order_id": "rejected-order",
                              "event_id": "rejected", "status": "rejected", "quantity": 200,
                              "cumulative_filled": 0}, now=NOW)
    stopped = store.stop_first_leg(p["id"], now=NOW)
    assert stopped["state"] == "cancelled"
    assert stopped["reserved_qty"] == 0 and stopped["reserved_cash"] == 0


def test_source_execution_identity_is_deduplicated_even_with_new_request_key(store):
    aid = account(store)["id"]
    position(store, aid)
    p = plan(store, aid)
    first = fill(store, p, 1, id="source-fill-1", key="request-a")
    second = fill(store, p, 1, id="source-fill-1", key="request-b")
    assert first["execution"]["id"] == second["execution"]["id"]
    assert store.get_position(aid, p["symbol"])["qty"] == 1200


@pytest.mark.parametrize("fields", [{"baseline_qty": 9999}, {"day_initial_sellable": 9999},
                                    {"effective_sellable": 9999}, {"enabled": "false"}])
def test_position_cannot_inject_internal_projection_fields(store, fields):
    aid = account(store)["id"]
    with pytest.raises(LedgerError):
        position(store, aid, **fields)


def test_active_plan_external_position_adjustment_keeps_exposure_and_requires_reconcile(store):
    aid = account(store)["id"]
    position(store, aid)
    p = plan(store, aid)
    fill(store, p, 1)
    acc, pos = store.get_account(aid), store.get_position(aid, p["symbol"])
    changed = store.upsert_position(aid, p["symbol"], {
        "qty": 1100, "sellable_qty": 900, "expected_version": pos["version"],
        "included_event_seq": acc["event_seq"], "reconciled": True,
    }, now=NOW)
    assert changed["qty"] == 1100
    assert store.get_plan(p["id"])["unmatched_qty"] == 200
    assert store.get_plan(p["id"])["reserved_qty"] == 200
    assert store.get_account(aid)["needs_reconciliation"]
    assert not store.get_account(aid)["benchmark_valid"]


def test_core_quantity_can_exceed_same_day_sellable(store):
    aid = account(store)["id"]
    pos = position(store, aid, sellable_qty=0, core_qty=800)
    assert pos["core_qty"] == 800 and pos["effective_sellable"] == 0


def test_overnight_unfinished_plan_still_consumes_next_days_risk_budget(store):
    aid = account(store)["id"]
    position(store, aid)
    first = plan(store, aid)
    fill(store, first, 1)
    tomorrow = NOW + timedelta(days=1)
    acc = store.get_account(aid)
    store.update_snapshot(aid, {"trade_date": "2026-10-20", "snapshot_at": tomorrow.isoformat(),
        "reconciled": True, "expected_version": acc["version"], "included_event_seq": acc["event_seq"]}, now=tomorrow)
    store.update_config(aid, {"daily_risk_budget": 80}, now=tomorrow)
    position(store, aid, "600001.SH", trade_date="2026-10-20", snapshot_at=tomorrow.isoformat())
    sig = signal(store, aid, "600001.SH", created_at=tomorrow.isoformat(),
                 expires_at=(tomorrow + timedelta(seconds=60)).isoformat())
    with pytest.raises(LedgerError, match="当日累计风险"):
        store.create_plan(sig["id"], 200, idempotency_key="day-two", now=tomorrow)


def test_refresh_without_cash_confirmation_cannot_clear_pending_proceeds(store):
    aid = account(store, cash=500)["id"]
    position(store, aid)
    p = plan(store, aid, direction="sell_buy")
    fill(store, p, 1)
    before = store.get_account(aid)
    after = store.update_snapshot(aid, {"expected_version": before["version"],
        "included_event_seq": before["event_seq"], "snapshot_at": NOW.isoformat()}, now=NOW)
    assert after["pending_proceeds"] == before["pending_proceeds"]
    assert "回补资金待核对" in store.get_plan(p["id"])["reasons"]


def test_first_known_sellable_is_conservative_daily_baseline_without_reconstructing_inventory(store):
    aid = account(store)["id"]
    initial = position(store, aid, sellable_qty=None, today_bought=200, today_sold=100)
    assert initial["day_initial_sellable"] is None
    acc = store.get_account(aid)
    confirmed = store.upsert_position(aid, initial["symbol"], {
        "sellable_qty": 700, "expected_version": initial["version"], "included_event_seq": acc["event_seq"],
        "reconciled": True,
    }, now=NOW)
    assert confirmed["day_initial_sellable"] == 700
    assert confirmed["day_inventory_basis"] == "first_confirmed_available"
    assert plan(store, aid)["quantity"] == 200


def test_views_expose_same_daily_limits_as_atomic_acceptance(store):
    aid = account(store)["id"]
    position(store, aid)
    p = plan(store, aid)
    acc, pos = store.get_account(aid), store.get_position(aid, p["symbol"])
    assert acc["daily_plans"] == pos["daily_plans"] == 1
    assert acc["daily_risk_used"] == p["risk_reserved"]
    assert pos["daily_t_qty"] == 200
    assert pos["cooldown_until"] == (NOW + timedelta(minutes=15)).isoformat()
    fill(store, p, 1, qty=100)
    store.stop_first_leg(p["id"], now=NOW)
    assert store.get_position(aid, p["symbol"])["daily_t_qty"] == 100
    fill(store, p, 2, qty=100, price=9)
    done = store.get_plan(p["id"])
    assert store.get_account(aid)["daily_risk_used"] == -done["matched_pnl"]


def test_overnight_fill_with_expired_fee_schedule_keeps_facts_but_withholds_net_profit(store):
    aid = account(store)["id"]
    store.update_config(aid, {"fee_effective_to": "2026-10-19"}, now=NOW)
    position(store, aid)
    p = plan(store, aid)
    fill(store, p, 1)
    tomorrow = NOW + timedelta(days=1)
    acc = store.get_account(aid)
    store.update_snapshot(aid, {"trade_date": "2026-10-20", "snapshot_at": tomorrow.isoformat(),
        "reconciled": True, "expected_version": acc["version"], "included_event_seq": acc["event_seq"]}, now=tomorrow)
    result = store.record_execution({"plan_id": p["id"], "leg": 2, "order_id": "tomorrow-sell",
        "quantity": 200, "price": 11, "executed_at": tomorrow.isoformat(), "proceeds_available": True,
        "idempotency_key": "overnight-expired-fee"}, now=tomorrow)
    assert result["plan"]["state"] == "completed"
    assert result["plan"]["matched_pnl"] is None
    assert result["execution"]["fee_unverified"]
    assert store.get_position(aid, p["symbol"])["qty"] == 1000
    assert store.get_account(aid)["needs_reconciliation"]
    assert store.get_account(aid)["cash_estimated"]
    review = store.review(aid, prices={p["symbol"]: 11})
    assert review["matched_pnl"] is None and review["fees"] is None
    assert review["incremental_equity"] is None
    corrected = store.correct_execution(result["execution"]["id"], {
        "fee": 6.12, "reason": "跨日实际手续费核对", "idempotency_key": "correct-next-day-fee"}, now=tomorrow)
    assert not corrected["execution"]["fee_unverified"]
    assert corrected["plan"]["matched_pnl"] is not None
    assert not store.review(aid)["fees_unknown"]


def test_expired_schedule_does_not_override_explicit_actual_fee(store):
    aid = account(store)["id"]
    store.update_config(aid, {"fee_effective_to": "2026-10-19"}, now=NOW)
    position(store, aid)
    p = plan(store, aid)
    fill(store, p, 1)
    tomorrow = NOW + timedelta(days=1)
    result = store.record_execution({"plan_id": p["id"], "leg": 2, "order_id": "actual-fee-order",
        "quantity": 200, "price": 11, "fee": 6.12, "executed_at": tomorrow.isoformat(),
        "proceeds_available": True, "idempotency_key": "known-next-day-fee"}, now=tomorrow)
    assert result["execution"]["allocated_fee"] == 6.12
    assert not result["execution"]["fee_unverified"]
    assert result["plan"]["matched_pnl"] == pytest.approx(188.86)
