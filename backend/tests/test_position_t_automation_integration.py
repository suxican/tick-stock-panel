from datetime import timedelta
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_position_t_rules import sample_context
from test_position_t_service import service as service_fixture

from app.api.position_t import router
from app.strategy.position_t import as_cn


@pytest.fixture
def service(tmp_path):
    return service_fixture.__wrapped__(tmp_path)


def simulation_account(service, direction="buy_sell"):
    bars, quote, environment, position, account, now, config = sample_context(direction)
    service.market.data.update(bars=[{**bar, "end": bar["datetime"].isoformat()} for bar in bars],
                               quote=quote, environment=environment)
    simulated = service.store.create_account({**account, "name": "自动模拟测试", "mode": "simulation",
        "config": {**config, "auto_simulation_enabled": True}}, now=now)
    service.store.upsert_position(simulated["id"], position["symbol"],
        {**{k: v for k, v in position.items() if k not in {"version", "effective_sellable"}},
         "snapshot_at": now.isoformat()}, now=now)
    service.market.data["quote"]["book_market_ts"] = now
    return simulated


def advance_minutes(service, minutes, price):
    for _ in range(minutes):
        service.market.now += timedelta(minutes=1)
        end = service.market.now.replace(second=0, microsecond=0)
        service.market.data["bars"].append({"end": end.isoformat(), "datetime": end.isoformat(),
            "open": price, "high": price + .05, "low": price - .05, "close": price,
            "volume_shares": 20000, "amount": price * 20000, "complete": True})
    now = service.market.now
    service.market.data["quote"].update(price=price, bid=round(price-.01, 2), ask=round(price+.01, 2),
        market_ts=now, received_ts=now, book_market_ts=now)
    service.market.data["environment"].update(market_ts=now, sector_ts=now)


@pytest.mark.parametrize("direction,first_price,target_price", [("buy_sell", 9.98, 10.35), ("sell_buy", 10.02, 9.65)])
def test_service_automatically_completes_delayed_two_leg_simulation(service, direction, first_price, target_price):
    account = simulation_account(service, direction)
    service.run_once()
    plans = service.store.list_plans(account["id"])
    assert len(plans) == 1
    plan = plans[0]
    assert plan["execution_mode"] == "automatic_simulation"
    assert service.store.plan_detail(plan["id"])["executions"] == []
    advance_minutes(service, 2, first_price)
    service.run_once()
    filled = service.store.get_plan(plan["id"])
    assert filled["leg1_filled"] == 200, (service.store.plan_detail(plan["id"]), service.simulation.status_by_account)
    position = service.store.get_position(account["id"], plan["symbol"])
    assert position["qty"] == (1200 if direction == "buy_sell" else 800)
    assert position["sellable_qty"] == (1000 if direction == "buy_sell" else 800)
    advance_minutes(service, 1, target_price)
    service.run_once()
    assert service.store.get_plan(plan["id"])["leg2_filled"] == 0
    advance_minutes(service, 2, target_price)
    service.run_once()
    detail = service.store.plan_detail(plan["id"])
    assert detail["plan"]["state"] == "completed"
    assert detail["plan"]["matched_pnl"] > 0
    assert all(x["source"] == "simulation_auto" for x in detail["executions"])
    position = service.store.get_position(account["id"], plan["symbol"])
    assert position["qty"] == 1000 and position["sellable_qty"] == 800
    assert len(service.store.list_plans()) == 1
    service.market.now = as_cn("2026-10-16T15:00:00+08:00")
    service.run_once()
    report = service.daily_review_report(account["id"], "2026-10-16")
    assert report["status"] == "final"
    assert report["summary"]["realized_net"] == detail["plan"]["matched_pnl"]
    assert report["summary"]["closed_today_count"] == 1
    assert report["coverage"]["source_counts"] == {"simulation_auto": 2}
    assert report["unfinished"] == []


def test_daily_report_finalizes_after_close_without_running_simulation(service, monkeypatch):
    account = simulation_account(service)
    service.run_once()
    service.market.now = as_cn("2026-10-16T15:00:00+08:00")
    for method in ("prepare", "process_pending", "submit"):
        monkeypatch.setattr(service.simulation, method, Mock(side_effect=AssertionError("must not simulate after close")))
    service.market.collect = Mock(side_effect=AssertionError("must not collect after close"))
    service.run_once()
    report = service.daily_review_report(account["id"], "2026-10-16")
    assert report["status"] == "final"
    assert report["summary"]["trade_count"] == 0
    assert report["valuation"]["close_equity"] is None
    assert service.overview(account["id"])["simulation"]["state"] == "idle"


def test_api_auto_toggle_versions_manual_guard_and_daily_report(service):
    app = FastAPI()
    app.state.position_t_service = service
    app.include_router(router)
    client = TestClient(app)
    manual = service.store.list_accounts()[0]
    assert client.put(f"/api/position-t/accounts/{manual['id']}/config", json={
        "expected_version": manual["version"], "auto_simulation_enabled": True}).status_code == 422
    account = simulation_account(service)
    response = client.put(f"/api/position-t/accounts/{account['id']}/config", json={
        "expected_version": account["version"], "auto_simulation_enabled": False})
    assert response.status_code == 409  # adding a position advanced the account version
    latest = service.store.get_account(account["id"])
    response = client.put(f"/api/position-t/accounts/{account['id']}/config", json={
        "expected_version": latest["version"], "auto_simulation_enabled": False})
    assert response.status_code == 200
    assert client.get("/api/position-t/overview", params={"account_id": account["id"]}).json()["simulation"]["state"] == "disabled"
    response = client.get("/api/position-t/daily-reviews", params={"account_id": account["id"], "date": "2026-10-16"})
    assert response.status_code == 200
    assert response.json()["report"]["status"] == "preliminary"
    assert client.get("/api/position-t/daily-reviews", params={"account_id": account["id"], "date": "2026-02-30"}).status_code == 422


def test_report_failure_keeps_monitor_idle_and_retries_without_quotes(service, monkeypatch):
    service.market.now = as_cn("2026-10-16T15:00:00+08:00")
    service.market.collect = Mock(side_effect=AssertionError("must not collect after close"))
    finalize = Mock(side_effect=[OSError("temporary report failure"), []])
    monkeypatch.setattr(service.daily_review, "finalize_due", finalize)
    service.run_once()
    assert service.health["status"] == "idle"
    assert any("复盘暂不可用" in reason for reason in service.health["reasons"])
    service.run_once()
    assert finalize.call_count == 2
    assert service.health["status"] == "idle"
    assert not any("复盘暂不可用" in reason for reason in service.health["reasons"])
