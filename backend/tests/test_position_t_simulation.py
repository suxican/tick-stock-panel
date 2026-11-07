from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_position_t_rules import sample_context

from app.services.position_t_simulation import PositionTSimulation
from app.services.position_t_store import LedgerError, PositionTStore
from app.strategy.position_t import as_cn, evaluate_candidate, order_fee


@pytest.fixture
def sim(tmp_path):
    bars, quote, env, pos, account, now, cfg = sample_context()
    store = PositionTStore(tmp_path)
    account = store.create_account({**account, "mode": "simulation",
        "config": {**cfg, "auto_simulation_enabled": True}}, now=now)
    store.upsert_position(account["id"], pos["symbol"], {k: v.isoformat() if hasattr(v, "isoformat") else v for k, v in pos.items()
        if k not in {"effective_sellable", "version"}}, now=now)
    quote["book_market_ts"] = now
    state = SimpleNamespace(store=store, now=now, aid=account["id"], symbol=pos["symbol"],
        data={"bars": bars, "quote": quote, "environment": env})
    state.engine = PositionTSimulation(store, lambda day: day.weekday() < 5, lambda: state.now)
    state.engine.prepare(now)
    return state


def outputs(s):
    return {(s.aid, s.symbol): deepcopy(s.data)}


def submit_first(s):
    a, p = s.store.get_account(s.aid), s.store.get_position(s.aid, s.symbol)
    signal = evaluate_candidate(s.data["bars"], s.data["quote"], s.data["environment"], p, a, s.now, "buy_sell", a["config"])
    assert signal["state"] == "triggered", signal
    s.store.save_signals([signal], now=s.now)
    s.engine.submit([signal], outputs(s), s.now)
    return s.store.list_plans()[0]


def advance(s, minutes=2, volume=10000, low=9.95, high=10.05):
    s.now += timedelta(minutes=minutes)
    end = s.now.replace(second=0, microsecond=0)
    s.data["bars"].append({"end": end.isoformat(), "open": 10, "high": high, "low": low,
        "close": 10, "volume_shares": volume, "amount": volume * 10, "complete": True})
    s.data["quote"].update(market_ts=s.now, received_ts=s.now, book_market_ts=s.now, minute_received_at=s.now)
    return s.data["bars"][-1]


def test_opt_in_and_manual_account_guards(sim):
    with pytest.raises(LedgerError, match="模拟"):
        sim.store.create_account({"mode": "manual", "config": {"auto_simulation_enabled": True}}, now=sim.now)
    manual = sim.store.create_account({"mode": "manual"}, now=sim.now)
    with pytest.raises(LedgerError, match="模拟"):
        sim.store.update_config(manual["id"], {"auto_simulation_enabled": True}, now=sim.now)


def test_order_waits_for_full_future_minute_and_fill_is_idempotent(sim):
    plan = submit_first(sim)
    assert plan["execution_mode"] == "automatic_simulation"
    assert not sim.store.plan_detail(plan["id"])["executions"]
    sim.engine.process_pending(outputs(sim), sim.now)
    advance(sim, minutes=1)
    sim.engine.process_pending(outputs(sim), sim.now)
    assert not sim.store.plan_detail(plan["id"])["executions"]
    advance(sim, minutes=1)
    assert sim.engine.process_pending(outputs(sim), sim.now)
    detail = sim.store.plan_detail(plan["id"])
    assert len(detail["executions"]) == 1
    assert detail["executions"][0]["source"] == "simulation_auto"
    assert detail["executions"][0]["price"] == 10
    assert detail["plan"]["first_stopped"]
    sim.engine.process_pending(outputs(sim), sim.now)
    assert len(sim.store.plan_detail(plan["id"])["executions"]) == 1
    assert sim.store.get_position(sim.aid, sim.symbol)["sellable_qty"] == 1000


def test_partial_first_cancels_remainder_and_preserves_exposure(sim):
    plan = submit_first(sim)
    advance(sim, volume=7500)
    sim.engine.process_pending(outputs(sim), sim.now)
    detail = sim.store.plan_detail(plan["id"])
    assert detail["plan"]["unmatched_qty"] == 150
    assert detail["plan"]["first_stopped"]
    assert detail["orders"][0]["status"] == "cancelled"
    assert detail["orders"][0]["cancelled_quantity"] == 50
    assert detail["plan"]["reserved_qty"] == 150


@pytest.mark.parametrize("low,high", [(10, 10.05), (10, 10)])
def test_touch_or_one_price_bar_does_not_fill(sim, low, high):
    plan = submit_first(sim)
    advance(sim, low=low, high=high)
    sim.engine.process_pending(outputs(sim), sim.now)
    assert not sim.store.plan_detail(plan["id"])["executions"]


def test_disable_cancels_only_owned_orders_and_never_fills(sim):
    plan = submit_first(sim)
    sim.store.update_config(sim.aid, {"auto_simulation_enabled": False}, now=sim.now)
    immediate = sim.store.plan_detail(plan["id"])
    assert immediate["orders"][0]["status"] == "cancelled"
    assert immediate["plan"]["reserved_cash"] == 0
    advance(sim)
    sim.engine.prepare(sim.now)
    sim.engine.process_pending(outputs(sim), sim.now)
    detail = sim.store.plan_detail(plan["id"])
    assert not detail["executions"]
    assert detail["orders"][0]["status"] == "cancelled"
    assert detail["plan"]["reserved_cash"] == 0


def test_restart_does_not_fill_an_offline_historical_bar(sim):
    plan = submit_first(sim)
    advance(sim)
    engine = PositionTSimulation(sim.store, lambda day: True, lambda: sim.now)
    engine.prepare(sim.now)
    engine.process_pending(outputs(sim), sim.now)
    assert not sim.store.plan_detail(plan["id"])["executions"]


def test_day_roll_unlocks_simulated_inventory_once_and_preserves_baseline(sim):
    plan = submit_first(sim)
    advance(sim)
    sim.engine.process_pending(outputs(sim), sim.now)
    before = sim.store.get_position(sim.aid, sim.symbol)
    baseline = sim.store.get_account(sim.aid)["baseline_cash"]
    sim.now = as_cn("2026-10-19T09:30:00+08:00")
    sim.engine.prepare(sim.now)
    position = sim.store.get_position(sim.aid, sim.symbol)
    assert position["qty"] == before["qty"] == position["sellable_qty"]
    assert position["today_bought"] == position["today_sold"] == 0
    assert position["baseline_qty"] == before["baseline_qty"]
    assert sim.store.get_account(sim.aid)["baseline_cash"] == baseline
    assert sim.store.get_plan(plan["id"])["unmatched_qty"] == 200
    sim.engine.prepare(sim.now)
    assert sim.store.get_position(sim.aid, sim.symbol)["sellable_qty"] == position["sellable_qty"]


def test_unknown_calendar_or_external_frozen_balance_never_refreshes(sim):
    before = sim.store.get_account(sim.aid)["snapshot_at"]
    sim.now += timedelta(minutes=20)
    sim.engine.calendar = lambda day: None
    sim.engine.prepare(sim.now)
    assert sim.store.get_account(sim.aid)["snapshot_at"] == before
    sim.engine.calendar = lambda day: True
    with sim.store._tx() as db:
        account = sim.store._get(db, "accounts", sim.aid)
        account["broker_frozen_cash"] = 100
        sim.store._save_account(db, account)
    sim.engine.prepare(sim.now)
    assert sim.store.get_account(sim.aid)["snapshot_at"] == before
    assert sim.engine.status_by_account[sim.aid]["state"] == "paused"


def test_expired_first_order_does_not_fill_even_if_minute_penetrates(sim):
    plan = submit_first(sim)
    advance(sim, minutes=6)
    sim.engine.process_pending(outputs(sim), sim.now)
    assert not sim.store.plan_detail(plan["id"])["executions"]
    assert sim.store.get_plan(plan["id"])["first_stopped"]


def test_last_full_valid_minute_fills_before_receipt_time_expiry(sim):
    plan = submit_first(sim)
    advance(sim, minutes=5, volume=5000)
    sim.engine.process_pending(outputs(sim), sim.now)
    detail = sim.store.plan_detail(plan["id"])
    assert detail["plan"]["leg1_filled"] == 100
    assert detail["plan"]["first_stopped"]
    assert detail["orders"][0]["cancelled_quantity"] == 100


@pytest.mark.parametrize("field,value", [("amount", 1), ("volume_shares", float("nan")), ("close", float("inf")), ("complete", False)])
def test_invalid_minute_never_fills_pending_order(sim, field, value):
    plan = submit_first(sim)
    advance(sim)[field] = value
    sim.engine.process_pending(outputs(sim), sim.now)
    assert not sim.store.plan_detail(plan["id"])["executions"]


@pytest.mark.parametrize("change", ["cash", "inventory", "disabled"])
def test_atomic_fill_rechecks_mutation_after_market_validation(sim, monkeypatch, change):
    plan = submit_first(sim)
    advance(sim)
    original = sim.store.fill_simulation_order
    def concurrent_change(*args, **kwargs):
        with sim.store._tx() as db:
            if change == "inventory":
                position = sim.store._position(db, sim.aid, sim.symbol)
                position["qty"] = position["target_qty"] + position["max_extra_qty"]
                sim.store._save_position(db, position)
            else:
                account = sim.store._get(db, "accounts", sim.aid)
                if change == "cash":
                    account["available_cash"] = 10
                else:
                    account["config"]["auto_simulation_enabled"] = False
                sim.store._save_account(db, account)
        return original(*args, **kwargs)
    monkeypatch.setattr(sim.store, "fill_simulation_order", concurrent_change)
    sim.engine.process_pending(outputs(sim), sim.now)
    assert not sim.store.plan_detail(plan["id"])["executions"]
    assert sim.store.get_plan(plan["id"])["leg1_filled"] == 0


def ready_second(s, plan_id):
    a, p = s.store.get_account(s.aid), s.store.get_position(s.aid, s.symbol)
    plan = s.store.get_plan(plan_id)
    s.data["quote"].update(price=10.30, bid=10.30, ask=10.31,
        market_ts=s.now, received_ts=s.now, book_market_ts=s.now)
    s.store.update_plan_monitor(plan_id, {"state": "ready", "risk_codes": [], "reasons": [],
        "plan_version": plan["version"], "account_version": a["version"], "position_version": p["version"],
        "expires_at": (s.now+timedelta(seconds=10)).isoformat(), "quote": {}, "computed_at": s.now.isoformat(),
        "second_leg_condition": "到价", "actionable_price": 10.30, "net_profit": 30}, now=s.now)


def test_partial_second_fill_charges_minimum_commission_once_and_t_plus_one(sim):
    plan = submit_first(sim)
    advance(sim)
    sim.engine.process_pending(outputs(sim), sim.now)
    ready_second(sim, plan["id"])
    sim.engine.submit([], outputs(sim), sim.now)
    assert len(sim.store.plan_detail(plan["id"])["executions"]) == 1
    for minutes in (2, 1):
        bar = advance(sim, minutes=minutes, volume=5000, low=10.2, high=10.4)
        bar.update(open=10.3, close=10.3, amount=51500)
        sim.engine.process_pending(outputs(sim), sim.now)
    detail = sim.store.plan_detail(plan["id"])
    second = [x for x in detail["executions"] if x["leg"] == 2]
    assert len(second) == 2
    assert sum(x["allocated_fee"] for x in second) == pytest.approx(float(order_fee("sell", 10.29, 200, plan["config"], trade_date=sim.now.date())))
    assert detail["plan"]["state"] == "completed"
    position = sim.store.get_position(sim.aid, sim.symbol)
    assert position["qty"] == 1000 and position["sellable_qty"] == 800


def test_stop_first_cancels_automatic_order_even_without_quotes(sim):
    plan = submit_first(sim)
    sim.store.stop_first_leg(plan["id"], now=sim.now)
    detail = sim.store.plan_detail(plan["id"])
    assert detail["orders"][0]["status"] == "cancelled"
    assert detail["plan"]["reserved_cash"] == detail["plan"]["reserved_qty"] == 0


def test_manual_plan_and_unaccounted_terminal_order_are_not_taken_over(sim):
    a, p = sim.store.get_account(sim.aid), sim.store.get_position(sim.aid, sim.symbol)
    signal = evaluate_candidate(sim.data["bars"], sim.data["quote"], sim.data["environment"], p, a, sim.now, "buy_sell", a["config"])
    sim.store.save_signals([signal], now=sim.now)
    plan = sim.store.create_plan(signal["id"], 200, idempotency_key="manual", now=sim.now)
    sim.store.record_order_event({"plan_id": plan["id"], "leg": 1, "order_id": "external", "event_id": "external-filled",
        "quantity": 200, "cumulative_filled": 200, "status": "filled", "occurred_at": sim.now.isoformat()}, now=sim.now)
    before = sim.store.get_account(sim.aid)["snapshot_at"]
    sim.now += timedelta(seconds=61)
    sim.engine.prepare(sim.now)
    sim.engine.submit([signal], outputs(sim), sim.now)
    assert sim.store.get_account(sim.aid)["snapshot_at"] == before
    detail = sim.store.plan_detail(plan["id"])
    assert detail["plan"]["execution_mode"] == "manual"
    assert detail["orders"][0].get("source") != "simulation_auto"
    assert not detail["executions"]


def test_process_rechecks_clock_immediately_before_fill(sim, monkeypatch):
    plan = submit_first(sim)
    advance(sim)
    original = sim.engine._latest_bar
    def boundary(*args):
        bar = original(*args)
        sim.now = as_cn("2026-10-16T11:30:00+08:00")
        return bar
    monkeypatch.setattr(sim.engine, "_latest_bar", boundary)
    sim.engine.process_pending(outputs(sim), sim.now)
    assert not sim.store.plan_detail(plan["id"])["executions"]


def set_risk(s, plan_id):
    plan = s.store.get_plan(plan_id)
    account = s.store.get_account(s.aid)
    position = s.store.get_position(s.aid, s.symbol)
    s.store.update_plan_monitor(plan_id, {"state": "risk", "risk_codes": ["price_invalid"], "reasons": ["方向失效"],
        "plan_version": plan["version"], "account_version": account["version"], "position_version": position["version"],
        "expires_at": (s.now+timedelta(seconds=10)).isoformat(), "quote": {}, "computed_at": s.now.isoformat(),
        "second_leg_condition": "处理敞口", "actionable_price": 9.70, "net_profit": -60}, now=s.now)


def test_risk_cancels_old_target_then_places_one_future_risk_order(sim):
    plan = submit_first(sim)
    advance(sim)
    sim.engine.process_pending(outputs(sim), sim.now)
    ready_second(sim, plan["id"])
    sim.engine.submit([], outputs(sim), sim.now)
    sim.now += timedelta(seconds=1)
    sim.data["quote"].update(price=9.70, bid=9.70, ask=9.71, market_ts=sim.now, received_ts=sim.now, book_market_ts=sim.now)
    set_risk(sim, plan["id"])
    sim.engine.submit([], outputs(sim), sim.now)
    orders = sim.store.plan_detail(plan["id"])["orders"]
    assert orders[-1]["purpose"] == "target" and orders[-1]["status"] == "cancelled"
    set_risk(sim, plan["id"])
    sim.engine.submit([], outputs(sim), sim.now)
    orders = sim.store.plan_detail(plan["id"])["orders"]
    assert orders[-1]["purpose"] == "risk" and orders[-1]["status"] == "submitted"
    set_risk(sim, plan["id"])
    sim.engine.submit([], outputs(sim), sim.now)
    assert len(sim.store.plan_detail(plan["id"])["orders"]) == 3
    bar = advance(sim, low=9.6, high=9.8)
    bar.update(open=9.7, close=9.7, amount=97000)
    sim.engine.process_pending(outputs(sim), sim.now)
    assert sim.store.get_plan(plan["id"])["state"] == "completed"


def test_stale_quote_does_not_cancel_target_or_place_risk_order(sim):
    plan = submit_first(sim)
    advance(sim)
    sim.engine.process_pending(outputs(sim), sim.now)
    ready_second(sim, plan["id"])
    sim.engine.submit([], outputs(sim), sim.now)
    sim.now += timedelta(seconds=11)
    set_risk(sim, plan["id"])
    sim.engine.submit([], outputs(sim), sim.now)
    assert sim.store.plan_detail(plan["id"])["orders"][-1]["status"] == "submitted"
    assert len(sim.store.plan_detail(plan["id"])["orders"]) == 2


def test_disable_after_close_immediately_cancels_second_order_but_preserves_exposure(sim):
    plan = submit_first(sim)
    advance(sim)
    sim.engine.process_pending(outputs(sim), sim.now)
    ready_second(sim, plan["id"])
    sim.engine.submit([], outputs(sim), sim.now)
    sim.now = as_cn("2026-10-16T15:00:00+08:00")
    sim.store.update_config(sim.aid, {"enabled": False}, now=sim.now)
    detail = sim.store.plan_detail(plan["id"])
    assert detail["orders"][-1]["status"] == "cancelled"
    assert detail["plan"]["unmatched_qty"] == detail["plan"]["reserved_qty"] == 200
    assert len(detail["executions"]) == 1


def test_atomic_failure_rolls_back_order_and_execution_then_retry_fills_once(sim, monkeypatch):
    plan = submit_first(sim)
    advance(sim)
    original = sim.store._record_execution
    def broken(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated crash before commit")
    monkeypatch.setattr(sim.store, "_record_execution", broken)
    with pytest.raises(RuntimeError, match="simulated crash"):
        sim.engine.process_pending(outputs(sim), sim.now)
    detail = sim.store.plan_detail(plan["id"])
    assert not detail["executions"] and detail["orders"][0]["status"] == "submitted"
    assert sim.store.get_position(sim.aid, sim.symbol)["qty"] == 1000
    monkeypatch.setattr(sim.store, "_record_execution", original)
    sim.engine.process_pending(outputs(sim), sim.now)
    sim.engine.process_pending(outputs(sim), sim.now)
    assert len(sim.store.plan_detail(plan["id"])["executions"]) == 1


def test_rollover_requires_confirmed_original_trading_day(sim):
    previous = sim.store.get_account(sim.aid)["snapshot_at"]
    sim.now = as_cn("2026-10-19T09:30:00+08:00")
    sim.engine.calendar = lambda day: True if day == sim.now.date() else None
    sim.engine.prepare(sim.now)
    assert sim.store.get_account(sim.aid)["snapshot_at"] == previous
    assert sim.engine.status_by_account[sim.aid]["state"] == "paused"


def test_expiring_one_plan_preserves_other_symbol_pending_order(sim):
    first = submit_first(sim)
    original = sim.store.get_signal(first["signal_id"])
    sim.now += timedelta(minutes=2)
    other_symbol = "600000.SH"
    _, _, _, position, _, _, _ = sample_context()
    position.update(symbol=other_symbol, exchange="SH", snapshot_at=sim.now.isoformat())
    position = {k: v for k, v in position.items() if k not in {"effective_sellable", "version"}}
    pos = sim.store.upsert_position(sim.aid, other_symbol, position, now=sim.now)
    account = sim.store.get_account(sim.aid)
    signal = {**original, "id": "second-signal", "symbol": other_symbol, "account_version": account["version"],
        "position_version": pos["version"], "created_at": sim.now.isoformat(), "expires_at": (sim.now+timedelta(seconds=10)).isoformat()}
    sim.store.save_signals([signal], now=sim.now)
    second = sim.store.create_plan(signal["id"], 200, idempotency_key="second-auto", now=sim.now, automatic_simulation=True)
    sim.data["quote"].update(market_ts=sim.now, received_ts=sim.now, book_market_ts=sim.now)
    sim.engine._submit_order(second, 1, sim.data, sim.now)
    sim.now += timedelta(minutes=3)
    sim.engine.process_pending({}, sim.now)
    assert sim.store.get_plan(first["id"])["state"] == "cancelled"
    assert sim.store.plan_detail(second["id"])["orders"][0]["status"] == "submitted"
    assert not sim.store.get_plan(second["id"])["first_stopped"]
