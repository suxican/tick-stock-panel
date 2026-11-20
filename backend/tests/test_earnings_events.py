"""业绩事件: 按声明数据集的数据源取数, 汇总为近期事件证据; 来源不可用不当作无事件。"""
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

from app.services import earnings_events as ev

TODAY = date(2026, 11, 20)
RAW = {"600519.SH": {
    "forecast": {"report_period": "2026-09-30", "announce_date": "2026-10-15", "type": "预增",
                 "change_min": 0.3, "change_max": 0.5},
    "express": {"report_period": "2026-09-30", "announce_date": "2026-10-20", "net_profit": 1.2e9,
                "net_profit_yoy": 0.2},
    "disclosures": [{"report_period": "2026-09-30", "scheduled_date": "2026-10-30", "actual_date": "2026-10-29"},
                    {"report_period": "2026-12-31", "scheduled_date": "2026-11-25", "actual_date": None}]}}


def _sources(monkeypatch, provider, declared=("plugin_x",)):
    monkeypatch.setattr(ev.custom_sources, "names", lambda: {"plugin_x", "other"})
    monkeypatch.setattr(ev.custom_sources, "provider_has_dataset", lambda name, ds: name in declared)
    monkeypatch.setattr(ev.custom_sources, "get_provider", lambda name: provider)
    ev._cache.clear()


def test_fetch_uses_declaring_source_and_caches(monkeypatch):
    provider = SimpleNamespace(get_earnings_events=Mock(return_value=RAW))
    _sources(monkeypatch, provider)
    assert ev.fetch_events(["600519.SH"], TODAY) == RAW
    assert ev.fetch_events(["600519.SH"], TODAY) == RAW
    provider.get_earnings_events.assert_called_once_with(["600519.SH"], TODAY)


def test_fetch_unavailable_without_source_or_on_error(monkeypatch):
    _sources(monkeypatch, SimpleNamespace(), declared=())
    assert ev.fetch_events(["600519.SH"], TODAY) is None
    _sources(monkeypatch, SimpleNamespace(get_earnings_events=Mock(side_effect=RuntimeError("down"))))
    assert ev.fetch_events(["600519.SH"], TODAY) is None


def test_summary_flags_disclosure_within_holding_horizon():
    summary = ev.summarize(RAW["600519.SH"], TODAY, horizon_end=date(2026, 11, 27))
    assert summary["available"] is True
    assert summary["next_disclosure"] == {"report_period": "2026-12-31", "date": "2026-11-25"}
    assert summary["disclosure_in_horizon"] is True
    assert ev.summarize(RAW["600519.SH"], TODAY, horizon_end=date(2026, 11, 24))["disclosure_in_horizon"] is False
    text = ev.event_evidence(summary, TODAY)
    assert text == ("近期事件（截至 2026-11-20，数据源已披露版本）：业绩预告 2026-09-30 报告期 预增，"
                    "净利润同比 +30%～+50%（2026-10-15 公告）；业绩快报 2026-09-30 报告期 净利润同比 +20%"
                    "（2026-10-20 公告）；下一次定期报告 2026-12-31 报告期 预约 2026-11-25 披露，在计划持有期内")


def test_summary_none_and_unavailable_texts():
    empty = ev.summarize({"forecast": None, "express": None, "disclosures": []}, TODAY, horizon_end=None)
    assert ev.event_evidence(empty, TODAY) == ("近期事件（截至 2026-11-20，数据源已披露版本）：近 120 天无业绩预告；"
                                               "近 120 天无业绩快报；下一次定期报告预约披露日未公布")
    missing = ev.summarize(None, TODAY, horizon_end=None)
    assert missing == {"available": False}
    assert ev.event_evidence(missing, TODAY) == "近期事件：业绩事件来源不可用，不能视为无事件"
