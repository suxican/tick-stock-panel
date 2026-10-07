from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest
from fastapi import HTTPException

from app.api import monitor_rules as monitor_rules_api
from app.services import alert_store
from app.strategy import monitor_rules
from app.strategy.monitor import MonitorRuleEngine


def _rule(source="first_board", **overrides):
    return monitor_rules.normalize({
        "id": "mode_rule", "name": "模式提醒", "type": source,
        "scope": "all", "mode_events": ["buy_candidate"] if source == "first_board" else ["a0_confirmed"],
        "cooldown_seconds": 60, **overrides,
    })


def _event(event_id="event_1", **overrides):
    return {
        "id": event_id, "type": "buy_candidate", "symbol": "600000.SH",
        "name": "浦发银行", "price": 10.5, "change_pct": 0.05,
        "message": "首板候选", "evidence": {"score": 80}, **overrides,
    }


def test_mode_rule_defaults_validation_and_non_mode_cleanup():
    first = monitor_rules.normalize({"id": "r", "name": "首板", "type": "first_board"})
    assert first["scope"] == "all"
    assert first["mode_events"] == ["buy_candidate", "broken", "exit_candidate"]
    monitor_rules.validate(first)
    assert monitor_rules.normalize({"type": "huichun"})["mode_events"] == ["a0_confirmed"]
    assert "mode_events" not in monitor_rules.normalize({"type": "price", "mode_events": ["buy_candidate"]})
    for overrides, message in [
        ({"mode_events": []}, "至少选择"),
        ({"mode_events": ["a0_confirmed"]}, "非法事件"),
        ({"mode_events": "buy_candidate"}, "至少选择"),
        ({"asset_type": "etf"}, "仅支持个股"),
        ({"conditions": [{"field": "close", "op": ">", "value": 1}]}, "不支持行情"),
    ]:
        with pytest.raises(ValueError, match=message):
            monitor_rules.validate(_rule(**overrides))


def test_mode_events_filter_scope_and_preserve_decimal_units():
    handled = []
    engine = MonitorRuleEngine(handled.append)
    engine.set_rules([
        _rule(scope="symbols", symbols=["600000.SH"]),
        _rule("huichun", id="huichun"),
        _rule(id="disabled", enabled=False),
    ])
    result = engine.evaluate_mode_events("first_board", [
        _event(), _event("outside", symbol="000001.SZ"), _event("unselected", type="sealed"),
    ], now=1000)
    assert len(result) == 1
    assert handled == result
    assert result[0]["rule_id"] == "mode_rule"
    assert result[0]["source_event_id"] == "event_1"
    assert result[0]["source"] == "first_board"
    assert result[0]["change_pct"] == 0.05
    assert result[0]["evidence"] == {"score": 80}
    assert engine.evaluate(pl.DataFrame({"symbol": ["600000.SH"], "close": [10.5]})) == []
    assert engine.evaluate_mode_events("unknown", [_event()]) == []
    assert engine.evaluate_mode_events("first_board", [_event(id="")]) == []


def test_mode_cooldown_event_dedupe_rule_edit_and_new_rule():
    engine = MonitorRuleEngine()
    rule = _rule(mode_events=["buy_candidate", "broken"])
    engine.set_rules([rule])
    assert len(engine.evaluate_mode_events("first_board", [_event()], now=1000)) == 1
    assert engine.evaluate_mode_events("first_board", [_event()], now=2000) == []
    assert engine.evaluate_mode_events("first_board", [_event("next")], now=1020) == []
    assert len(engine.evaluate_mode_events("first_board", [_event("broken", type="broken")], now=1020)) == 1
    assert len(engine.evaluate_mode_events("first_board", [_event("next")], now=1061)) == 1
    engine.set_rules([{**rule, "cooldown_seconds": 0}])
    assert engine.evaluate_mode_events("first_board", [_event()], now=1062) == []
    assert len(engine.evaluate_mode_events("first_board", [_event("new")], now=1062)) == 1
    engine.set_rules([{**rule, "id": "new_rule"}])
    assert len(engine.evaluate_mode_events("first_board", [_event()], now=1063)) == 1


def test_mode_dedupe_restored_from_alert_store(tmp_path):
    engine = MonitorRuleEngine()
    engine.set_data_dir(tmp_path)
    engine.set_rules([_rule("huichun")])
    event = _event("a0_20261007", type="a0_confirmed")
    first = engine.evaluate_mode_events("huichun", [event])
    alert_store.append_many(tmp_path, first)
    restarted = MonitorRuleEngine()
    restarted.set_data_dir(tmp_path)
    restarted.set_rules([_rule("huichun")])
    assert restarted.evaluate_mode_events("huichun", [event]) == []
    assert len(restarted.evaluate_mode_events("huichun", [_event("next_day", type="a0_confirmed")], now=first[0]["ts"] / 1000 + 61)) == 1


def test_mode_concurrency_only_claims_event_once():
    engine = MonitorRuleEngine()
    engine.set_rules([_rule(cooldown_seconds=0)])
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: engine.evaluate_mode_events("first_board", [_event()]), range(20)))
    assert sum(len(result) for result in results) == 1


def test_mode_dedupe_expires_with_alert_retention_and_clear_requires_restart(tmp_path):
    engine = MonitorRuleEngine()
    engine.set_data_dir(tmp_path)
    engine.set_rules([_rule(cooldown_seconds=0)])
    events = engine.evaluate_mode_events("first_board", [_event()])
    alert_store.append_many(tmp_path, events)
    alert_store.clear(tmp_path)
    assert engine.evaluate_mode_events("first_board", [_event()]) == []
    restarted = MonitorRuleEngine()
    restarted.set_data_dir(tmp_path)
    restarted.set_rules([_rule(cooldown_seconds=0)])
    assert len(restarted.evaluate_mode_events("first_board", [_event()])) == 1
    future = events[0]["ts"] / 1000 + (alert_store.MAX_DAYS + 1) * 86400
    assert len(engine.evaluate_mode_events("first_board", [_event()], now=future)) == 1


def test_mode_callback_failure_isolated_from_other_rules():
    handler = Mock(side_effect=RuntimeError("notification offline"))
    engine = MonitorRuleEngine(handler)
    engine.set_rules([_rule(), _rule(id="second")])
    assert len(engine.evaluate_mode_events("first_board", [_event()])) == 2
    assert handler.call_count == 2


def test_mode_failed_publication_can_retry_original_event():
    engine = MonitorRuleEngine()
    engine.set_rules([_rule()])
    first = engine.evaluate_mode_events("first_board", [_event()], now=1000)
    engine.rollback_mode_events(first)
    retried = engine.evaluate_mode_events("first_board", [_event()], now=1000)
    assert len(retried) == 1
    # 重复处理第一次失败不能撤销已经重试的新占位, 即使毫秒时间戳相同。
    engine.rollback_mode_events(first)
    assert engine.evaluate_mode_events("first_board", [_event()], now=1001) == []


def test_mode_rollback_keeps_later_events_cooldown():
    engine = MonitorRuleEngine()
    engine.set_rules([_rule(cooldown_seconds=0)])
    first = engine.evaluate_mode_events("first_board", [_event()], now=1000)
    later = engine.evaluate_mode_events("first_board", [_event("later")], now=1000)
    engine.rollback_mode_events(first)
    # 与后续事件同时触发, 撤销早先事件仍不能清除后续事件的冷却。
    engine.rules["mode_rule"]["cooldown_seconds"] = 60
    assert engine.evaluate_mode_events("first_board", [_event("third")], now=1001) == []
    assert engine.evaluate_mode_events("first_board", [_event("later")], now=1061) == []
    engine.rollback_mode_events(later)
    assert len(engine.evaluate_mode_events("first_board", [_event("third")], now=1001)) == 1


def test_mode_restore_respects_cooldown_for_distinct_event(tmp_path):
    engine = MonitorRuleEngine()
    engine.set_data_dir(tmp_path)
    engine.set_rules([_rule()])
    events = engine.evaluate_mode_events("first_board", [_event()])
    alert_store.append_many(tmp_path, events)
    restarted = MonitorRuleEngine()
    restarted.set_data_dir(tmp_path)
    restarted.set_rules([_rule()])
    now = events[0]["ts"] / 1000 + 10
    assert restarted.evaluate_mode_events("first_board", [_event("different")], now=now) == []


def test_mode_group_missing_and_changed_members_fail_closed(monkeypatch):
    engine = MonitorRuleEngine()
    engine.set_rules([_rule(scope="watchlist_group", group_id="g1", cooldown_seconds=0)])
    members = Mock(return_value=set())
    monkeypatch.setattr("app.strategy.monitor._group_members_or_none", members)
    assert engine.evaluate_mode_events("first_board", [_event()]) == []
    members.return_value = {"600000.SH"}
    assert len(engine.evaluate_mode_events("first_board", [_event()])) == 1
    members.return_value = None
    assert engine.evaluate_mode_events("first_board", [_event("next")]) == []


def test_mode_configured_rules_include_disabled_and_follow_crud():
    engine = MonitorRuleEngine()
    engine.set_rules([_rule(enabled=False)])
    assert engine.has_mode_rules("first_board")
    assert not engine.has_rule_type("first_board")
    engine.remove_rule("mode_rule")
    assert not engine.has_mode_rules("first_board")
    engine.add_rule(_rule("huichun", enabled=False))
    assert engine.has_mode_rules("huichun")
    engine.clear()
    assert not engine.has_mode_rules("huichun")


def test_mode_api_save_reload_and_invalid_event(tmp_path):
    engine = MonitorRuleEngine()
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo, monitor_engine=engine)))
    result = monitor_rules_api.save_rule(monitor_rules_api.RuleModel(**_rule("huichun")), request)
    assert result["rule"]["mode_events"] == ["a0_confirmed"]
    assert monitor_rules.load_one(tmp_path, "mode_rule")["mode_events"] == ["a0_confirmed"]
    assert engine.has_mode_rules("huichun")
    with pytest.raises(HTTPException) as exc:
        monitor_rules_api.save_rule(monitor_rules_api.RuleModel(**_rule("huichun", mode_events=["buy_candidate"])), request)
    assert exc.value.status_code == 400


def test_mode_api_options_and_empty_rule_list(tmp_path):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))
    assert monitor_rules_api.list_rules(request) == {"rules": []}
    options = monitor_rules_api.get_options(request)
    assert {"key": "first_board", "label": "首板模式"} in options["types"]
    assert options["mode_events"]["huichun"] == [
        {"key": "a0_confirmed", "label": "A0 日线确认"},
        {"key": "pending_cross", "label": "待金叉观察"},
    ]
    saved = monitor_rules_api.save_rule(
        monitor_rules_api.RuleModel(id="mode_default", name="回春提醒", type="huichun"), request,
    )
    assert saved["rule"]["scope"] == "all"
    assert saved["rule"]["mode_events"] == ["a0_confirmed"]


@pytest.mark.parametrize("service, expected", [
    (None, "服务未就绪"),
    (SimpleNamespace(get_config=lambda: {"enabled": False, "notify": True}), "自动盯盘已关闭"),
    (SimpleNamespace(get_config=lambda: {"enabled": True, "notify": False}), "消息通知已关闭"),
    (SimpleNamespace(get_config=lambda: {"enabled": True, "notify": True}), None),
    (SimpleNamespace(), None),
])
def test_mode_api_runtime_warnings(tmp_path, service, expected):
    monitor_rules.save_one(tmp_path, _rule())
    monitor_rules.save_one(tmp_path, _rule("huichun", id="huichun"))
    monitor_rules.save_one(tmp_path, _rule(id="disabled", enabled=False))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    state = SimpleNamespace(repo=repo, first_board_service=service)
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    rules = {rule["id"]: rule for rule in monitor_rules_api.list_rules(request)["rules"]}
    if expected:
        assert expected in rules["mode_rule"]["runtime_warning"]
    else:
        assert "runtime_warning" not in rules["mode_rule"]
    assert "回春模式服务未就绪" in rules["huichun"]["runtime_warning"]
    assert "runtime_warning" not in rules["disabled"]
    state.huichun_mode_service = SimpleNamespace()
    rules = {rule["id"]: rule for rule in monitor_rules_api.list_rules(request)["rules"]}
    assert "runtime_warning" not in rules["huichun"]
