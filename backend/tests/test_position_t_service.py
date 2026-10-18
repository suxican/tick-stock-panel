from copy import deepcopy
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_position_t_rules import sample_context

from app.api.position_t import router
from app.services.position_t_service import PositionTService, trading_seconds
from app.services.position_t_store import LedgerError
from app.strategy.position_t import as_cn


class Market:
    def __init__(self, context):
        bars, quote, env, _, _, now, _ = context
        self.data = {"bars": [{**b, "end": b["datetime"].isoformat()} for b in bars],
                     "quote": quote, "environment": env}
        self.now, self.fail = now, False

    def collect(self, positions, accounts, now):
        if self.fail:
            raise OSError("upstream unavailable")
        # 模拟provider已标准化JSON契约，不向用户运行目录写数据。
        def json_safe(obj):
            if isinstance(obj, dict):
                return {k: json_safe(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [json_safe(x) for x in obj]
            return obj.isoformat() if hasattr(obj, "isoformat") else obj
        return {(p["account_id"], p["symbol"]): json_safe(deepcopy(self.data)) for p in positions}

    def trading_day(self, day):
        return day.weekday() < 5

    def capabilities(self):
        return {"manual": True, "simulation": True, "broker": False, "auto_order": False}

    def sessions(self, first, last):
        return [first] if first == last and self.trading_day(first) else []


@pytest.fixture
def service(tmp_path):
    ctx = sample_context()
    market = Market(ctx)
    notes = []
    service = PositionTService(tmp_path, market, clock=lambda: market.now, notifier=notes.append)
    _, _, _, pos, account, now, cfg = ctx
    a = service.store.create_account({**account, "name": "测试账户", "config": cfg}, now=now)
    clean_pos = {k: v for k, v in pos.items() if k not in {"effective_sellable", "version"}}
    service.store.upsert_position(a["id"], pos["symbol"], {**clean_pos, "snapshot_at": now.isoformat()}, now=now)
    service.notes = notes
    return service


def triggered(service):
    service.run_once()
    return next(s for s in service.store.list_signals() if s["state"] == "triggered")


def accept(service, signal):
    payload = {"signal_id": signal["id"], "quantity": 200,
               "expected_account_version": signal["account_version"], "expected_position_version": signal["position_version"],
               "idempotency_key": "request-a"}
    return service.accept_plan(payload), payload


def test_background_candidate_does_not_reserve_and_deduplicates(service):
    signal = triggered(service)
    aid = signal["account_id"]
    assert service.store.get_account(aid)["reserved_cash"] == 0
    assert service.store.list_plans() == []
    service.market.now += timedelta(seconds=1)
    service.run_once()
    assert len(service.notes) == 1
    assert len(service.store.list_signals()) == 2
    assert service.health["last_run"]


def test_accept_retry_is_idempotent_after_monitor_has_blocked_new_signals(service):
    signal = triggered(service)
    plan, payload = accept(service, signal)
    service.run_once()
    assert service.accept_plan(payload)["id"] == plan["id"]
    assert len(service.store.list_plans()) == 1
    with pytest.raises(LedgerError, match="幂等"):
        service.accept_plan({**payload, "quantity": 100})


def test_outage_invalidates_signal_but_preserves_fill_and_inventory_reservation(service):
    signal = triggered(service)
    plan, _ = accept(service, signal)
    service.store.record_execution({"plan_id": plan["id"], "leg": 1, "order_id": "actual1", "quantity": 100,
        "price": 9.99, "executed_at": service.market.now.isoformat(), "idempotency_key": "fill1"}, now=service.market.now)
    service.market.fail = True
    service.run_once()
    actual = service.store.get_plan(plan["id"])
    assert actual["unmatched_qty"] == 100
    assert actual["reserved_qty"] >= 100
    assert "data_unavailable" in actual["monitor"]["risk_codes"]
    assert all(s["state"] != "triggered" for s in service.store.list_signals())
    assert service.health["status"] == "paused"


def test_changed_quote_rejects_accept_without_resources(service):
    signal = triggered(service)
    service.market.data["quote"]["ask"] = 10.99
    with pytest.raises(LedgerError, match="变化"):
        accept(service, signal)
    assert not service.store.list_plans()


def test_stop_first_on_expiry_releases_only_uncommitted_resources(service):
    plan, _ = accept(service, triggered(service))
    service.market.now += timedelta(minutes=6)
    service.run_once()
    actual = service.store.get_plan(plan["id"])
    assert actual["state"] == "cancelled"
    assert actual["reserved_cash"] == actual["reserved_qty"] == 0


def test_trading_clock_excludes_lunch_and_weekend(service):
    start = as_cn("2026-10-16T11:20:00+08:00")
    end = as_cn("2026-10-16T13:10:00+08:00")
    assert trading_seconds(start, end, service.market.trading_day) == 20 * 60
    assert trading_seconds(as_cn("2026-10-16T14:57:00+08:00"), as_cn("2026-10-19T09:40:00+08:00"), service.market.trading_day) == 600


def test_partial_odd_quantity_requires_explicit_second_leg_handling(service):
    ctx = sample_context("sell_buy")
    service.market.data.update(bars=[{**b, "end": b["datetime"].isoformat()} for b in ctx[0]], quote=ctx[1], environment=ctx[2])
    plan, _ = accept(service, triggered(service))
    service.store.record_execution({"plan_id": plan["id"], "leg": 1, "order_id": "partial150", "quantity": 150,
        "price": 10.01, "executed_at": service.market.now.isoformat(), "idempotency_key": "fill150", "proceeds_available": True}, now=service.market.now)
    service.store.stop_first_leg(plan["id"], now=service.market.now)
    service.run_once()
    actual = service.store.get_plan(plan["id"])
    assert actual["unmatched_qty"] == 150
    assert "quantity_unavailable" in actual["monitor"]["risk_codes"]


def test_stale_snapshot_pauses_and_restart_expires_remaining_signal(service):
    signal = triggered(service)
    service.market.now += timedelta(minutes=16)
    service.run_once()
    assert any("快照过期" in " ".join(s["reasons"]) for s in service.store.list_signals())
    service.store.save_signals([{**signal, "state": "triggered"}], now=service.market.now)
    restarted = PositionTService(service.data_dir, service.market, clock=lambda: service.market.now)
    assert restarted.store.get_signal(signal["id"])["state"] == "expired"


def test_api_separates_orders_from_execution_and_rejects_injected_fields(service):
    app = FastAPI()
    app.state.position_t_service = service
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/api/position-t/capabilities").json()["auto_order"] is False
    plan, _ = accept(service, triggered(service))
    response = client.post("/api/position-t/order-events", json={"plan_id": plan["id"], "leg": 1, "order_id": "order-api",
        "event_id": "event-api", "status": "submitted", "quantity": 200, "price": 9.99,
        "occurred_at": service.market.now.isoformat()})
    assert response.status_code == 200, response.text
    detail = client.get(f"/api/position-t/plans/{plan['id']}").json()
    assert detail["events"][0]["status"] == "submitted"
    assert detail["executions"] == []
    assert detail["plan"]["leg1_filled"] == 0
    invalid = client.post("/api/position-t/executions", json={"plan_id": plan["id"], "leg": 1, "order_id": "order-api",
        "quantity": 100, "price": 9.99, "executed_at": service.market.now.isoformat(),
        "idempotency_key": "bad", "verified": True})
    assert invalid.status_code == 422


def test_api_requires_versions_and_aware_timestamps(service):
    app = FastAPI()
    app.state.position_t_service = service
    app.include_router(router)
    client = TestClient(app)
    aid = service.store.list_accounts()[0]["id"]
    assert client.put(f"/api/position-t/accounts/{aid}/config", json={"enabled": True}).status_code == 422
    response = client.post("/api/position-t/accounts", json={"name": "无时区", "mode": "manual", "trade_date": "2026-10-16", "snapshot_at": "2026-10-16T10:00:00"})
    assert response.status_code == 422
    assert client.post("/api/position-t/replays", json={"account_id": aid, "symbol": "000001.SZ", "start_date": "2026-10-20", "end_date": "2026-10-16"}).status_code == 422


def test_recorded_evidence_roundtrips_into_isolated_same_day_replay(service):
    signal = triggered(service)
    initial = service.store.get_account(signal["account_id"])
    payload = {"account_id": signal["account_id"], "symbol": signal["symbol"], "start_date": "2026-10-16", "end_date": "2026-10-16"}
    result = service.replay(payload)
    assert result["source_mode"] == "simulation"
    assert result["data_source"] == "recorded_observations"
    assert service.store.get_account(initial["id"]) == initial
    assert service.store.list_plans() == []
    with pytest.raises(LedgerError, match="单交易日"):
        service.replay({**payload, "end_date": "2026-10-19"})
