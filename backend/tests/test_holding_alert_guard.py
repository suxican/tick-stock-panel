"""研究提醒默认拒绝；有效性检查不占用行情回调线程。"""
import threading
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import polars as pl

from app.market_time import cn_now
from app.services.holding_alert_guard import HoldingAlertGuard
from app.services.holding_analysis_store import HoldingAnalysisStore
from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine


def test_guard_requires_current_source_plan_and_live_worker(tmp_path, monkeypatch):
    now = cn_now()
    store = HoldingAnalysisStore(tmp_path)
    keys = ["h_test"]
    report = store.save_report({
        "source": "lots", "account_id": "", "input_version": "v1",
        "profile_version": store.profile_version(keys), "generated_at": now.isoformat(),
        "as_of_date": now.date().isoformat(),
        "rows": [{"key": keys[0], "symbol": "600000.SH", "risk_status": {"items": [], "fingerprint": "clean"}, "price": {
            "date": now.date().isoformat(), "reference_date": (now - timedelta(days=1)).date().isoformat(), "reference_close": 10},
                  "plan": {"can_adopt": True, "expires_on": now.date().isoformat()}}],
    })
    plan = store.adopt_plan(report["id"], keys[0], "v1")
    pending = store.set_alert_rule(plan["id"], plan["version"], "ha_test")
    store.activate_alert_rule(plan["id"], pending["version"], "ha_test")
    monitor_rules.save_one(tmp_path, {"id": "ha_test", "enabled": True, "holding_plan_id": plan["id"]})
    inputs = {"input_version": "v1", "positions": [{"key": keys[0]}]}
    monkeypatch.setattr("app.services.holding_alert_guard.read_inputs", lambda *_a: inputs)
    risk = {"fingerprint": "clean"}
    monkeypatch.setattr("app.services.holding_analysis.current_risk", lambda _s, _now=None, **_k: {"items": [], **risk})
    clock = [now]
    quote = {"600000.SH": {"timestamp": int(now.timestamp() * 1000), "last_price": 10, "prev_close": 10}}
    guard = HoldingAlertGuard(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
                              SimpleNamespace(get_market_records=lambda _s: quote,
                                              status=lambda: {"enabled": True, "realtime_allowed": True}), None,
                              clock=lambda: clock[0])
    rule = {"id": "ha_test", "holding_plan_id": plan["id"]}
    assert not guard.allow(rule, now.timestamp())
    guard.refresh()
    assert guard.allow(rule, now.timestamp())
    risk["fingerprint"] = "st"  # 采纳后风险状态变化: 提醒暂停
    guard.refresh()
    assert not guard.allow(rule, now.timestamp())
    risk["fingerprint"] = "clean"
    guard.refresh()
    assert guard.allow(rule, now.timestamp())
    quote["600000.SH"]["prev_close"] = 9.5
    guard.refresh()
    assert not guard.allow(rule, now.timestamp())
    quote["600000.SH"]["prev_close"] = 10
    guard.refresh()
    assert not guard.allow(rule, (now + timedelta(seconds=11)).timestamp())
    inputs["input_version"] = "v2"
    guard.refresh()
    assert not guard.allow(rule, now.timestamp())
    inputs["input_version"] = "v1"
    quote["600000.SH"]["timestamp"] -= 60_000
    guard.refresh()
    # 报价新鲜度改由引擎在评估那一刻按所用报价判断; 守卫只管计划、版本、风险与昨收基准
    assert guard.allow(rule, now.timestamp())
    quote["600000.SH"]["timestamp"] = int(now.timestamp() * 1000)
    clock[0] = now + timedelta(days=1)
    guard.refresh()
    assert not guard.allow(rule, clock[0].timestamp())


def test_engine_holding_rules_fail_closed_and_compare_raw_prices():
    engine = MonitorRuleEngine()
    rule = {"id": "ha_test", "name": "研究价格提醒", "type": "price", "enabled": True,
            "scope": "symbols", "symbols": ["600000.SH"], "holding_plan_id": "hap_test",
            "conditions": [{"field": "raw_close", "op": "<=", "value": 10}], "cooldown_seconds": 0}
    frame = pl.DataFrame({"symbol": ["600000.SH"], "close": [20.], "raw_close": [9.], "change_pct": [-.01],
                          "quote_ts": [95_000]})
    assert not engine._evaluate_rule(frame, rule, 100.)
    engine.set_holding_alert_guard(lambda _rule, _now: True)
    events = engine._evaluate_rule(frame, rule, 100.)
    assert len(events) == 1
    assert events[0]["price"] == 9
    engine.set_holding_alert_guard(lambda _rule, _now: False)
    assert not engine._evaluate_rule(frame, rule, 101.)
    assert not engine._evaluate_rule(frame.drop("raw_close"), rule, 102.)


def test_revoke_waits_for_old_refresh_before_clearing_admission(tmp_path, monkeypatch):
    now = cn_now()
    entered, release, revoked = threading.Event(), threading.Event(), threading.Event()
    plan = {"id": "hap_test", "status": "adopted", "source": "lots", "account_id": "",
            "generated_at": now.isoformat(), "expires_on": now.date().isoformat(),
            "input_version": "v1", "profile_version": "p1", "alert_rule_ids": ["ha_test"],
            "alert_bindings": {"ha_test": "active"}, "row": {"symbol": "600000.SH", "price": {
                "reference_date": now.date().isoformat(), "reference_close": 10}}}

    def status():
        entered.set()
        assert release.wait(2)
        return {"enabled": True, "realtime_allowed": True}

    quotes = SimpleNamespace(status=status, get_market_records=lambda _s: {
        "600000.SH": {"timestamp": now.timestamp() * 1000, "last_price": 10}})
    guard = HoldingAlertGuard(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), quotes, None, clock=lambda: now)
    monkeypatch.setattr(guard.store, "all_plans", lambda: [deepcopy(plan)])
    monkeypatch.setattr(guard.store, "profile_version", lambda _keys: "p1")
    monkeypatch.setattr("app.services.holding_alert_guard.read_inputs", lambda *_a: {
        "input_version": "v1", "positions": [{"key": "h_test"}]})
    monkeypatch.setattr(monitor_rules, "load_one", lambda *_a: {
        "enabled": True, "holding_plan_id": "hap_test"})
    worker = threading.Thread(target=guard.refresh)
    worker.start()
    assert entered.wait(2)
    plan["alert_bindings"]["ha_test"] = "pending"

    def revoke():
        guard.revoke()
        revoked.set()

    cancellation = threading.Thread(target=revoke)
    cancellation.start()
    try:
        assert not revoked.wait(.05)
    finally:
        release.set()
        worker.join(2)
        cancellation.join(2)
    assert revoked.is_set()
    assert not guard.allow({"id": "ha_test", "holding_plan_id": "hap_test"}, now.timestamp())


def test_engine_holding_rules_require_fresh_quote_at_evaluation():
    """全市场行情到手已晚 6-9 秒; 新鲜度在引擎评估时按触发所用的同一份报价判断 (不超过 10 秒)。"""
    engine = MonitorRuleEngine()
    engine.set_holding_alert_guard(lambda _rule, _now: True)
    rule = {"id": "ha_fresh", "name": "研究价格提醒", "type": "price", "enabled": True,
            "scope": "symbols", "symbols": ["600000.SH"], "holding_plan_id": "hap_test",
            "conditions": [{"field": "raw_close", "op": "<=", "value": 10}], "cooldown_seconds": 0}

    def frame(quote_ts):
        return pl.DataFrame({"symbol": ["600000.SH"], "close": [20.], "raw_close": [9.], "change_pct": [-.01],
                             "quote_ts": [quote_ts]}, schema_overrides={"quote_ts": pl.Int64})

    assert len(engine._evaluate_rule(frame(91_000), rule, 100.)) == 1  # 9 秒前: 放行
    assert not engine._evaluate_rule(frame(89_000), rule, 100.)  # 11 秒前: 不触发, 等下一轮
    assert not engine._evaluate_rule(frame(101_000), rule, 100.)  # 晚于评估时刻: 时间异常
    assert not engine._evaluate_rule(frame(None), rule, 100.)  # 缺报价时间: 不触发
    no_ts = pl.DataFrame({"symbol": ["600000.SH"], "close": [20.], "raw_close": [9.], "change_pct": [-.01]})
    assert not engine._evaluate_rule(no_ts, rule, 100.)
