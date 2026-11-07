import json
from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock

import pytest

from app.services.position_t_daily_review import PositionTDailyReview
from app.services.position_t_store import PositionTStore
from tests.test_position_t_simulation import advance, outputs, submit_first
from tests.test_position_t_simulation import sim as simulation_fixture
from tests.test_position_t_store import NOW, account, fill, plan, position


@pytest.fixture
def context(tmp_path):
    store = PositionTStore(tmp_path)
    aid = account(store)["id"]
    position(store, aid)
    reviews = PositionTDailyReview(store, lambda day: day.weekday() < 5)
    return store, aid, reviews


def test_minute_samples_replace_instead_of_counting_each_poll(context):
    store, aid, reviews = context
    item = {"account_id": aid, "symbol": "600000.SH", "direction": "buy_sell",
            "state": "blocked", "reasons": ["可卖不足"], "strategy_version": "test"}
    for seconds in (0, 5, 10):
        reviews.record_cycle([item], NOW + timedelta(seconds=seconds))
    result = reviews.get_report(aid, "2026-10-19", NOW + timedelta(seconds=15))
    assert result["status"] == "preliminary"
    assert result["coverage"]["sample_count"] == 1
    assert result["blockers"] == [{"reason": "可卖不足", "count": 1}]
    assert result["summary"]["trade_count"] == 0
    assert result["summary"]["completion_rate"] is None
    assert store.list_plans() == []


def test_completed_trade_uses_matching_costs_without_fake_valuation(context):
    store, aid, reviews = context
    p = plan(store, aid)
    fill(store, p, 1, price=10)
    fill(store, p, 2, price=11)
    before = deepcopy(store.get_account(aid))
    result = reviews.get_report(aid, "2026-10-19", NOW.replace(hour=15, minute=5))
    summary = result["summary"]
    assert result["status"] == "final"
    assert summary["realized_net"] == pytest.approx(200 - summary["matched_fees"])
    assert summary["trade_fees"] == summary["matched_fees"]
    assert summary["started_count"] == summary["completed_count"] == summary["closed_today_count"] == 1
    assert summary["win_count"] == 1
    assert summary["completion_rate"] == 1
    assert result["unfinished"] == []
    assert result["valuation"]["close_equity"] is None
    assert result["valuation"]["max_drawdown"] is None
    assert result["source_mode"] == "manual"
    assert all(x["source"] == "manual_record" for x in result["trades"])
    assert store.get_account(aid) == before


def test_cross_day_second_leg_belongs_to_execution_day_not_plan_day(context):
    store, aid, reviews = context
    p = plan(store, aid)
    first = fill(store, p, 1, price=10)["execution"]
    tomorrow = NOW + timedelta(days=1)
    second = store.record_execution({"plan_id": p["id"], "leg": 2, "order_id": "next-day-sell",
        "quantity": 200, "price": 11, "executed_at": tomorrow.isoformat(),
        "idempotency_key": "next-day-fill"}, now=tomorrow)["execution"]
    yesterday = reviews.get_report(aid, "2026-10-19", tomorrow.replace(hour=15))
    today = reviews.get_report(aid, "2026-10-20", tomorrow.replace(hour=15))
    assert yesterday["summary"]["realized_net"] == 0
    assert yesterday["summary"]["trade_fees"] == first["allocated_fee"]
    assert yesterday["unfinished"][0]["exposure_qty"] == 200
    assert today["summary"]["realized_net"] == pytest.approx(200-first["allocated_fee"]-second["allocated_fee"])
    assert today["summary"]["trade_fees"] == second["allocated_fee"]
    assert today["summary"]["matched_fees"] == pytest.approx(first["allocated_fee"]+second["allocated_fee"])
    assert today["summary"]["started_count"] == 0
    assert today["summary"]["closed_today_count"] == 1
    assert today["summary"]["completion_rate"] is None


def test_partial_second_leg_keeps_unmatched_inventory_and_daily_pnl(context):
    store, aid, reviews = context
    p = plan(store, aid, direction="sell_buy")
    fill(store, p, 1, price=10)
    fill(store, p, 2, qty=100, price=9)
    result = reviews.get_report(aid, "2026-10-19", NOW.replace(hour=15))
    assert result["unfinished"][0]["unmatched_qty"] == 100
    assert result["unfinished"][0]["exposure_qty"] == -100
    assert result["summary"]["realized_net"] > 0
    assert result["summary"]["completed_count"] == 0
    assert result["by_direction"][0]["key"] == "sell_buy"
    assert any(x["code"] == "unfinished_risk" for x in result["suggestions"])


def test_accepted_plan_without_order_still_has_reserved_first_leg(context):
    store, aid, reviews = context
    p = plan(store, aid)
    report = reviews.get_report(aid, "2026-10-19", NOW.replace(hour=15))
    assert len(report["unfinished"]) == 1
    item = report["unfinished"][0]
    assert item["plan_id"] == p["id"]
    assert item["remaining_first_qty"] == 200
    assert item["pending_order_qty"] == item["exposure_qty"] == 0
    assert report["plans"][0]["remaining_first_qty"] == 200
    assert report["summary"]["trade_count"] == 0


def test_matched_partial_plan_keeps_first_leg_that_can_still_increase(context):
    store, aid, reviews = context
    p = plan(store, aid)
    fill(store, p, 1, qty=100, price=10)
    fill(store, p, 2, qty=100, price=11)
    assert store.get_plan(p["id"])["state"] == "first_partial"
    assert store.get_plan(p["id"])["reserved_qty"] == 100
    # 后一天补归档也必须保留尚未停止的首笔余量，不能仅按当天创建筛选。
    report = reviews.get_report(aid, "2026-10-20", (NOW + timedelta(days=1)).replace(hour=15))
    assert len(report["unfinished"]) == 1
    item = report["unfinished"][0]
    assert item["remaining_first_qty"] == 100
    assert item["unmatched_qty"] == item["exposure_qty"] == item["pending_order_qty"] == 0
    assert report["plans"][0]["remaining_first_qty"] == 100


def test_finalize_is_after_close_idempotent_and_restarts_from_local_records(context, monkeypatch):
    store, aid, reviews = context
    p = plan(store, aid)
    fill(store, p, 1)
    monkeypatch.setattr(store, "list_accounts", Mock(side_effect=AssertionError("must not scan accounts")))
    assert reviews.finalize_due(NOW.replace(hour=8)) == []
    assert reviews.finalize_due(NOW.replace(hour=14, minute=59)) == []
    archived = reviews.finalize_due(NOW.replace(hour=15))
    assert len(archived) == 1
    original_tx = store._tx
    monkeypatch.setattr(store, "_tx", Mock(side_effect=AssertionError("must not scan again")))
    assert reviews.finalize_due(NOW.replace(hour=16)) == []
    monkeypatch.setattr(store, "_tx", original_tx)
    restarted = PositionTDailyReview(store, reviews.calendar)
    assert restarted.finalize_due(NOW + timedelta(days=1)) == []
    assert restarted.get_report(aid, "2026-10-19", NOW + timedelta(days=1)) == archived[0]


def test_no_evidence_does_not_archive_empty_weekend_or_premarket_report(context):
    _store, aid, reviews = context
    result = reviews.get_report(aid, "2026-10-19", NOW.replace(hour=8))
    assert result["status"] == "preliminary"
    assert result["evidence_status"] == "insufficient"
    weekend = NOW.replace(day=18, hour=15)
    reviews.record_cycle([{"account_id": aid, "symbol": "600000.SH", "direction": "buy_sell",
                           "state": "blocked", "reasons": ["休市"]}], weekend)
    assert reviews.finalize_due(weekend) == []
    assert reviews.get_report(aid, "2026-10-18", NOW)["status"] == "preliminary"


def test_archived_report_is_frozen_and_explicit_about_later_fee_corrections(context):
    store, aid, reviews = context
    p = plan(store, aid)
    execution = fill(store, p, 1)["execution"]
    original = reviews.get_report(aid, "2026-10-19", NOW.replace(hour=15))
    store.correct_execution(execution["id"], {"fee": 8, "reason": "实际费用核对",
        "idempotency_key": "later-fee"}, now=NOW.replace(hour=16))
    assert reviews.get_report(aid, "2026-10-19", NOW.replace(hour=17)) == original
    assert any("不会自动回写" in text for text in original["limitations"])


def test_unknown_fee_withholds_verified_profit_and_fee(context):
    store, aid, reviews = context
    store.update_config(aid, {"fee_effective_to": "2026-10-19"}, now=NOW)
    p = plan(store, aid)
    fill(store, p, 1)
    tomorrow = NOW + timedelta(days=1)
    store.record_execution({"plan_id": p["id"], "leg": 2, "order_id": "later-order",
        "quantity": 200, "price": 11, "executed_at": tomorrow.isoformat(),
        "idempotency_key": "unknown-fee"}, now=tomorrow)
    report = reviews.get_report(aid, "2026-10-20", tomorrow.replace(hour=15))
    assert report["summary"]["realized_net"] is None
    assert report["summary"]["matched_fees"] is None
    assert report["summary"]["trade_fees"] is None
    assert report["summary"]["unknown_pnl_count"] == 1
    assert report["summary"]["win_count"] == 0
    assert report["summary"]["realized_net_provisional"] > 0


def test_report_counts_all_history_not_only_recent_api_limit(context):
    store, aid, reviews = context
    with store._tx() as db:
        for i in range(1005):
            event = {"signal": {"id": f"s{i}", "episode_id": f"episode{i}", "state": "triggered"},
                     "recorded_at": NOW.isoformat()}
            db.execute("INSERT INTO signal_history(account_id,symbol,trade_date,payload) VALUES(?,?,?,?)",
                       (aid, "600000.SH", "2026-10-19", json.dumps(event)))
    report = reviews.get_report(aid, "2026-10-19", NOW.replace(hour=15))
    assert report["coverage"]["triggered_episode_count"] == 1005
    assert report["coverage"]["unaccepted_episode_count"] == 1005
    assert report["summary"]["realized_net"] == 0
    assert report["unfinished"] == []
    assert any(s["code"] == "unaccepted_opportunities" for s in report["suggestions"])


def test_next_day_cancel_cannot_remove_prior_day_pending_order(context):
    store, aid, reviews = context
    p = plan(store, aid)
    event = {"plan_id": p["id"], "leg": 1, "order_id": "pending", "quantity": 200,
             "status": "submitted", "cumulative_filled": 0, "event_id": "submit", "occurred_at": NOW.isoformat()}
    store.record_order_event(event, now=NOW)
    tomorrow = NOW + timedelta(days=1)
    store.record_order_event({**event, "event_id": "cancel", "status": "cancelled", "cancelled_quantity": 200,
                              "occurred_at": tomorrow.isoformat()}, now=tomorrow)
    report = reviews.get_report(aid, "2026-10-19", tomorrow)
    assert report["unfinished"][0]["pending_order_qty"] == 200
    assert report["unfinished"][0]["exposure_qty"] == 0


def test_automatic_and_manually_registered_simulation_fills_are_distinct(context):
    store, _aid, reviews = context
    simulation = store.create_account({"name": "模拟", "mode": "simulation", "available_cash": 10000,
        "trade_date": "2026-10-19", "snapshot_at": NOW.isoformat(), "reconciled": True,
        "config": {"enabled": True, "fees_confirmed": True, "min_net_profit": 0,
                   "min_net_return": 0, "daily_quantity_ratio": 1}}, now=NOW)
    aid = simulation["id"]
    position(store, aid)
    p = plan(store, aid)
    first = fill(store, p, 1)["execution"]
    fill(store, p, 2)
    # 固定新执行器写入的来源契约，测试不依赖其运行时行情和撮合。
    with store._tx() as db:
        db.execute("UPDATE executions SET payload=json_set(payload,'$.source','simulation_auto') WHERE id=?", (first["id"],))
    report = reviews.get_report(aid, "2026-10-19", NOW.replace(hour=15))
    assert report["source_mode"] == "simulation"
    assert report["coverage"]["source_counts"] == {"simulation_auto": 1, "simulation_manual": 1}


def test_failed_finalization_retries_without_partial_archive(context, monkeypatch):
    store, aid, reviews = context
    fill(store, plan(store, aid), 1)
    build = reviews._build
    monkeypatch.setattr(reviews, "_build", Mock(side_effect=RuntimeError("disk busy")))
    with pytest.raises(RuntimeError, match="disk busy"):
        reviews.finalize_due(NOW.replace(hour=15))
    with store._tx() as db:
        assert db.execute("SELECT count(*) FROM daily_reviews").fetchone()[0] == 0
    monkeypatch.setattr(reviews, "_build", build)
    assert len(reviews.finalize_due(NOW.replace(hour=15))) == 1


def test_automatic_partial_fill_audit_rebuilds_only_real_exposure(tmp_path):
    sim = simulation_fixture.__wrapped__(tmp_path)
    reviews = PositionTDailyReview(sim.store, sim.engine.calendar)
    p = submit_first(sim)
    advance(sim, volume=7500)
    sim.engine.process_pending(outputs(sim), sim.now)
    report = reviews.get_report(sim.aid, sim.now.date().isoformat(), sim.now.replace(hour=15))
    assert report["unfinished"] == [{"plan_id": p["id"], "symbol": sim.symbol,
        "direction": "buy_sell", "first_qty": 150, "second_qty": 0,
        "unmatched_qty": 150, "exposure_qty": 150, "pending_order_qty": 0,
        "remaining_first_qty": 0, "state": "unfinished"}]
    assert report["trades"][0]["source"] == "simulation_auto"


def test_automatic_cancellation_audit_does_not_leave_phantom_pending_order(tmp_path):
    sim = simulation_fixture.__wrapped__(tmp_path)
    reviews = PositionTDailyReview(sim.store, sim.engine.calendar)
    submit_first(sim)
    sim.store.update_config(sim.aid, {"auto_simulation_enabled": False}, now=sim.now)
    sim.engine.prepare(sim.now)
    report = reviews.get_report(sim.aid, sim.now.date().isoformat(), sim.now.replace(hour=15))
    assert report["summary"]["trade_count"] == 0
    assert report["unfinished"] == []


def test_execution_wait_reasons_are_deduplicated_and_survive_restart(context):
    store, aid, reviews = context
    status = {aid: {"state": "waiting", "reasons": ["资金不足", "资金不足"]}}
    for seconds in (0, 5, 10):
        reviews.record_cycle([], NOW + timedelta(seconds=seconds), status)
    restarted = PositionTDailyReview(store, reviews.calendar)
    report = restarted.get_report(aid, "2026-10-19", NOW.replace(hour=15))
    assert report["coverage"]["sample_count"] == 0
    assert report["coverage"]["execution_sample_count"] == 1
    assert report["execution_blockers"] == [{"reason": "资金不足", "count": 1}]
    assert report["blockers"] == []
    assert report["summary"]["realized_net"] == 0
    assert report["status"] == "final"
    assert any(item["code"] == "execution_blocked" for item in report["suggestions"])


def test_execution_samples_keep_last_minute_state_without_inventing_missed_profit(context):
    _store, aid, reviews = context
    reviews.record_cycle([], NOW, {aid: {"state": "paused", "reasons": ["盘口过期"]}})
    reviews.record_cycle([], NOW + timedelta(seconds=5), {aid: {"state": "running", "reasons": []}})
    report = reviews.get_report(aid, "2026-10-19", NOW + timedelta(seconds=10))
    assert report["coverage"]["execution_sample_count"] == 0
    assert report["execution_blockers"] == []
    reviews.record_cycle([], NOW + timedelta(minutes=1), {aid: {"state": "waiting", "reasons": ["候选已失效"]}})
    reports = reviews.finalize_due(NOW.replace(hour=15))
    assert len(reports) == 1
    assert reports[0]["coverage"]["execution_sample_count"] == 1
    assert reports[0]["summary"]["trade_count"] == 0
