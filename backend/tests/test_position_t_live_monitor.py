from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_position_t_rules import sample_context
from test_position_t_service import accept, triggered
from test_position_t_service import service as service_fixture


@pytest.fixture
def service(tmp_path):
    return service_fixture.__wrapped__(tmp_path)


def first_fill(service, quantity=200, price=9.99):
    plan, _ = accept(service, triggered(service))
    service.store.record_execution({"plan_id": plan["id"], "leg": 1, "order_id": "first",
        "quantity": quantity, "price": price, "executed_at": service.market.now.isoformat(),
        "idempotency_key": "first"}, now=service.market.now)
    return plan


def target_quote(service, price=10.30):
    service.market.data["quote"].update(price=price, bid=price, ask=round(price + .01, 2),
        market_ts=service.market.now, received_ts=service.market.now,
        book_market_ts=service.market.now)


def test_second_leg_uses_bid_actual_fills_and_emits_once(service):
    plan = first_fill(service)
    target_quote(service)
    service.run_once()
    monitor = service.store.get_plan(plan["id"])["monitor"]
    assert monitor["state"] == "ready"
    assert monitor["actionable_price"] == 10.30
    assert monitor["net_profit"] > 20
    assert [n["payload"]["kind"] for n in service.notes].count("second_leg_ready") == 1
    service.run_once()
    assert [n["payload"]["kind"] for n in service.notes].count("second_leg_ready") == 1
    assert service.store.get_plan(plan["id"])["leg2_filled"] == 0


def test_second_leg_actual_cost_can_block_old_target(service):
    plan = first_fill(service, price=10.29)
    target_quote(service)
    service.run_once()
    monitor = service.store.get_plan(plan["id"])["monitor"]
    assert monitor["state"] != "ready"
    assert monitor["net_profit"] < 20


@pytest.mark.parametrize("mutation", [
    lambda s: s.market.data["quote"].update(book_market_ts=s.market.now-timedelta(seconds=11)),
    lambda s: s.market.data["quote"].update(trade_status="unknown"),
    lambda s: s.market.data["quote"].update(bid=None),
])
def test_second_leg_missing_execution_data_never_ready(service, mutation):
    plan = first_fill(service)
    target_quote(service)
    mutation(service)
    service.run_once()
    assert service.store.get_plan(plan["id"])["monitor"]["state"] == "paused"
    assert not any(n["payload"]["kind"] == "second_leg_ready" for n in service.notes)


def test_partial_first_expires_from_plan_creation(service):
    plan = first_fill(service, quantity=100)
    service.market.now += timedelta(minutes=6)
    target_quote(service)
    service.run_once()
    current = service.store.get_plan(plan["id"])
    assert current["first_stopped"]
    assert current["unmatched_qty"] == current["reserved_qty"] == 100
    assert current["reserved_cash"] == 0


def test_notification_reaches_quote_stream_and_retry_is_deduplicated(service, monkeypatch):
    from app.services import alert_store

    published = []
    service.market.quotes = SimpleNamespace(push_alerts=published.extend)
    service.notifier = service._notify
    monkeypatch.setattr(alert_store, "_publish_events", lambda events: None)
    triggered(service)
    assert len(published) == 1
    assert published[0]["source"] == "position_t"
    assert published[0]["account_id"]
    assert published[0]["expires_at"]


def test_intraminute_trigger_and_invalidation_keep_evidence(service):
    signal = triggered(service)
    service.market.now += timedelta(seconds=1)
    service.market.data["quote"].update(price=9.60, bid=9.59, ask=9.61)
    service.run_once()
    history = service.store.list_signal_history(signal["account_id"], signal["symbol"])
    buy = [e for e in history if e["signal"]["direction"] == "buy_sell"]
    assert buy[0]["signal"]["state"] == "triggered"
    assert buy[-1]["signal"]["state"] != "triggered"
    assert buy[0]["context"]["bars"]
    assert buy[-1]["context"]["quote"]["price"] == 9.60


def test_sell_buy_requires_ask_and_confirmed_proceeds(service):
    ctx = sample_context("sell_buy")
    service.market.data.update(bars=[{**b, "end": b["datetime"].isoformat()} for b in ctx[0]],
                               quote=ctx[1], environment=ctx[2])
    plan = first_fill(service, price=10.01)
    target_quote(service, 9.70)
    service.run_once()
    assert service.store.get_plan(plan["id"])["monitor"]["state"] != "ready"
    account = service.store.get_account(plan["account_id"])
    service.store.update_snapshot(account["id"], {"available_cash": account["available_cash"] + account["pending_proceeds"],
        "included_event_seq": account["event_seq"], "expected_version": account["version"], "reconciled": True,
        "snapshot_at": service.market.now.isoformat()}, now=service.market.now)
    service.run_once()
    monitor = service.store.get_plan(plan["id"])["monitor"]
    assert monitor["state"] == "ready"
    assert monitor["actionable_price"] == pytest.approx(9.71)


def test_pending_second_order_suppresses_duplicate_opportunity(service):
    plan = first_fill(service)
    service.store.record_order_event({"plan_id": plan["id"], "leg": 2, "order_id": "open-second",
        "event_id": "open-second", "status": "submitted", "quantity": 200, "price": 10.3,
        "occurred_at": service.market.now.isoformat()}, now=service.market.now)
    target_quote(service)
    service.run_once()
    monitor = service.store.get_plan(plan["id"])["monitor"]
    assert monitor["state"] != "ready"
    assert "未决委托" in monitor["second_leg_condition"]


def test_unstopped_partial_first_does_not_suggest_duplicate_exposure(service):
    plan = first_fill(service, quantity=100)
    target_quote(service, 10.5)
    service.run_once()
    assert service.store.get_plan(plan["id"])["monitor"]["state"] != "ready"


def test_remaining_cost_uses_fifo_and_only_future_leg_slippage(service):
    from app.services.position_t_service import _remaining_net

    plan = first_fill(service, quantity=100, price=9.90)
    service.store.record_execution({"plan_id": plan["id"], "leg": 1, "order_id": "first-b",
        "quantity": 100, "price": 10.10, "fee": 5, "executed_at": service.market.now.isoformat(),
        "idempotency_key": "first-b"}, now=service.market.now)
    service.store.record_execution({"plan_id": plan["id"], "leg": 2, "order_id": "second-a",
        "quantity": 100, "price": 10.30, "fee": 5, "executed_at": service.market.now.isoformat(),
        "idempotency_key": "second-a", "proceeds_available": True}, now=service.market.now)
    detail = service.store.plan_detail(plan["id"])
    # 同时成交的 FIFO 以稳定 ID 排序，按账本实际配对后的剩余成本验证。
    matched = {m["first_execution_id"] for m in detail["plan"]["matches"]}
    remaining = next(x for x in detail["executions"] if x["leg"] == 1 and x["id"] not in matched)
    net, basis, future = _remaining_net(detail["plan"], detail["executions"], 10.30, service.market.now.date())
    assert basis == pytest.approx(remaining["price"] * 100)
    assert net == pytest.approx(1030 - basis - remaining["allocated_fee"] - future)
    assert future == pytest.approx(6.53)


def test_stale_monitor_snapshot_cannot_emit_ready_after_execution(service):
    plan = first_fill(service)
    target_quote(service)
    service.run_once()
    previous = service.store.get_plan(plan["id"])["monitor"]
    service.store.record_execution({"plan_id": plan["id"], "leg": 2, "order_id": "done",
        "quantity": 200, "price": 10.30, "executed_at": service.market.now.isoformat(),
        "idempotency_key": "done", "proceeds_available": True}, now=service.market.now)
    result = service.store.update_plan_monitor(plan["id"], previous, now=service.market.now)
    assert result["state"] == "completed"
    assert result["monitor"]["state"] != "ready"


def test_account_reconciliation_immediately_invalidates_ready(service):
    plan = first_fill(service)
    target_quote(service)
    service.run_once()
    account = service.store.get_account(plan["account_id"])
    service.store.update_snapshot(account["id"], {"available_cash": account["available_cash"],
        "included_event_seq": account["event_seq"], "expected_version": account["version"], "reconciled": True,
        "snapshot_at": service.market.now.isoformat()}, now=service.market.now)
    assert service.store.get_plan(plan["id"])["monitor"]["state"] == "paused"


def test_partial_second_sell_does_not_require_its_proceeds_to_sell_remaining(service):
    plan = first_fill(service)
    service.store.record_execution({"plan_id": plan["id"], "leg": 2, "order_id": "partial-second",
        "quantity": 100, "price": 10.30, "executed_at": service.market.now.isoformat(),
        "idempotency_key": "partial-second"}, now=service.market.now)
    target_quote(service, 10.40)
    service.run_once()
    monitor = service.store.get_plan(plan["id"])["monitor"]
    assert monitor["state"] == "ready"
    assert monitor["unmatched_qty"] == 100


def test_ready_delivery_recovers_after_old_payload_expired(service):
    first_fill(service)
    target_quote(service)
    def fail(event):
        if event["payload"]["kind"] == "second_leg_ready":
            raise OSError("offline")
    service.notifier = fail
    with pytest.raises(OSError):
        service.run_once()
    service.market.now += timedelta(seconds=11)
    target_quote(service)
    service.notifier = service.notes.append
    service.run_once()
    assert len([n for n in service.notes if n["payload"]["kind"] == "second_leg_ready"]) == 1


def test_stream_failure_retries_same_event_without_duplicate_record(service, monkeypatch):
    from app.services import alert_store

    monkeypatch.setattr(alert_store, "_publish_events", lambda events: None)
    monkeypatch.setattr("time.time", lambda: service.market.now.timestamp())
    seen = []
    def push(alerts):
        seen.extend(alerts)
        if len(seen) == 1:
            raise OSError("stream unavailable")
    service.market.quotes = SimpleNamespace(push_alerts=push)
    service.notifier = service._notify
    with pytest.raises(OSError):
        triggered(service)
    assert service.store.pending_notifications()
    service._drain_notifications(service.market.now)
    assert seen[0]["event_id"] == seen[1]["event_id"]
    assert len(alert_store.list_recent(service.data_dir, source="position_t")) == 1
    assert not service.store.pending_notifications()


def test_failed_candidate_delivery_is_discarded_after_invalidation(service):
    def fail(event):
        raise OSError("notification unavailable")
    service.notifier = fail
    with pytest.raises(OSError):
        triggered(service)
    assert service.store.pending_notifications()
    service.market.now += timedelta(seconds=1)
    service.market.data["quote"].update(price=9.60, bid=9.59, ask=9.61)
    service.notifier = service.notes.append
    service.run_once()
    assert service.notes == []
    assert not service.store.pending_notifications()


def test_candidate_delivery_rechecks_changed_account_before_next_market_cycle(service):
    def fail(event):
        raise OSError("offline")
    service.notifier = fail
    with pytest.raises(OSError):
        triggered(service)
    account = service.store.list_accounts()[0]
    service.store.update_config(account["id"], {"enabled": False, "expected_version": account["version"]}, now=service.market.now)
    service.notifier = service.notes.append
    service._drain_notifications(service.market.now)
    assert service.notes == []
    assert not service.store.pending_notifications()


def test_history_endpoint_returns_evidence_and_bounds(service):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.position_t import router

    signal = triggered(service)
    app = FastAPI()
    app.state.position_t_service = service
    app.include_router(router)
    client = TestClient(app)
    query = {"account_id": signal["account_id"], "symbol": signal["symbol"], "date": "2026-10-16", "limit": 1}
    response = client.get("/api/position-t/signal-history", params=query)
    assert response.status_code == 200
    assert response.json()["events"][0]["context"]["bars"]
    assert client.get("/api/position-t/signal-history", params={**query, "limit": 1001}).status_code == 422


def test_ready_event_broadcasts_to_every_existing_quote_subscriber(service, monkeypatch):
    from app.services import alert_store
    from app.services.quote_service import QuoteService

    quotes = QuoteService()
    service.market.quotes = quotes
    left, right = quotes.subscribe(), quotes.subscribe()
    monkeypatch.setattr(alert_store, "_publish_events", lambda events: None)
    monkeypatch.setattr("time.time", lambda: service.market.now.timestamp())
    first_fill(service)
    service.notifier = service._notify
    target_quote(service)
    service.run_once()
    one, two = left.pop()["alerts"], right.pop()["alerts"]
    assert one == two
    assert any(e["kind"] == "second_leg_ready" and e["reference_price"] == 10.30 for e in one)
    assert left.pop()["alerts"] == []
