"""模式事件经过统一规则后才落盘和通知, 不进入模拟盘跟单。"""
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest

from app.services import alert_store
from app.services.quote_service import QuoteService
from app.strategy.monitor import MonitorRuleEngine
from app.strategy.monitor_rules import normalize


def configured_service(tmp_path, rules):
    engine = MonitorRuleEngine()
    engine.set_data_dir(tmp_path)
    engine.set_rules([normalize(rule) for rule in rules])
    service = QuoteService()
    service._repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    service._app_state = SimpleNamespace(monitor_engine=engine)
    service._broadcast_alerts = Mock()
    service._maybe_send_system_notifications = Mock()
    service._maybe_send_webhook = Mock()
    return service


def rule(source, **overrides):
    return {
        "id": "mr_mode", "name": "模式提醒", "type": source, "scope": "all",
        "mode_events": ["buy_candidate" if source == "first_board" else "a0_confirmed"],
        **overrides,
    }


def event(source, event_type, event_id="event1"):
    return {
        "id": event_id, "source": source, "type": event_type,
        "symbol": "600001.SH", "name": "测试股票", "message": "模式观察",
        "price": 10.5, "change_pct": 0.05,
    }


@pytest.mark.parametrize("source,event_type", [
    ("first_board", "buy_candidate"), ("huichun", "a0_confirmed"),
])
def test_mode_rule_routes_one_formatted_event_to_every_output(tmp_path, source, event_type):
    service = configured_service(tmp_path, [rule(source)])
    service._format_extension_notifications = lambda events: [
        {**item, "message": "统一中文通知"} for item in events
    ]
    publish = service.publish_first_board_alerts if source == "first_board" else service.publish_mode_alerts
    publish([event(source, event_type), event(source, "not_selected", "event2")])
    stored = alert_store.list_recent(tmp_path)
    assert len(stored) == 1
    assert stored[0]["rule_id"] == "mr_mode"
    assert stored[0]["source_event_id"] == "event1"
    assert stored[0]["price"] == 10.5 and stored[0]["change_pct"] == 0.05
    assert stored[0]["message"] == "统一中文通知"
    service._broadcast_alerts.assert_called_once_with(stored)
    service._maybe_send_system_notifications.assert_called_once_with(stored)
    service._maybe_send_webhook.assert_called_once_with(stored, service._app_state.monitor_engine)

    publish([event(source, event_type)])
    assert len(alert_store.list_recent(tmp_path)) == 1
    restored = configured_service(tmp_path, [rule(source)])
    restored.publish_mode_alerts([event(source, event_type)])
    restored._broadcast_alerts.assert_not_called()


def test_disabled_first_board_rule_does_not_fall_back_to_legacy_alerts(tmp_path):
    service = configured_service(tmp_path, [rule("first_board", enabled=False)])
    service.publish_first_board_alerts([event("first_board", "buy_candidate")])
    assert alert_store.list_recent(tmp_path) == []
    service._broadcast_alerts.assert_not_called()


def test_huichun_without_rules_is_quiet(tmp_path):
    service = configured_service(tmp_path, [])
    service.publish_mode_alerts([event("huichun", "a0_confirmed")])
    assert alert_store.list_recent(tmp_path) == []
    service._maybe_send_webhook.assert_not_called()


def test_quote_fetch_offers_snapshot_after_cache_update_outside_fetch_lock():
    service = QuoteService()
    daily = pl.DataFrame({"symbol": ["600001.SH"]})
    sequence = []

    def fetch(**kwargs):
        sequence.append("cache_updated")
        service._fetched_at += 1
        return daily, None

    def offer(snapshot):
        assert snapshot is daily
        assert not service._fetch_lock.locked()
        sequence.append("offered")

    service._fetch_full_market_quotes = Mock(side_effect=fetch)
    service._evaluate_monitors = Mock()
    service._offer_first_board_snapshot = Mock(side_effect=offer)
    assert service._fetch_quotes()
    assert sequence == ["cache_updated", "offered"]
    service._evaluate_monitors.assert_called_once_with(daily, None)


def test_failed_quote_fetch_does_not_offer_a_snapshot():
    service = QuoteService()
    service._fetch_full_market_quotes = Mock(return_value=None)
    service._offer_first_board_snapshot = Mock()
    assert not service._fetch_quotes()
    service._offer_first_board_snapshot.assert_not_called()


def test_failed_alert_write_can_retry_the_same_source_event(tmp_path, monkeypatch):
    service = configured_service(tmp_path, [rule("huichun")])
    source_event = event("huichun", "a0_confirmed")
    append = alert_store.append_many
    writes = Mock(side_effect=[OSError("disk unavailable"), None])

    def append_with_failure(data_dir, events):
        writes()
        append(data_dir, events)

    monkeypatch.setattr(alert_store, "append_many", append_with_failure)
    with pytest.raises(OSError, match="disk unavailable"):
        service.publish_mode_alerts([source_event])
    service._broadcast_alerts.assert_not_called()
    assert alert_store.list_recent(tmp_path) == []

    service.publish_mode_alerts([source_event])
    assert len(alert_store.list_recent(tmp_path)) == 1
    service._broadcast_alerts.assert_called_once()
