"""资金证据服务: 窗口只含已定版交易日; 三态不混淆; 不可用绝不输出 0; 缓存与回放可见性。"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.market_time import CN_TZ
from app.services import fund_evidence as fe

SYM = "600519.SH"
MORNING = datetime(2026, 11, 20, 10, 0, tzinfo=CN_TZ)      # 周五盘中
AFTER = datetime(2026, 11, 20, 15, 31, tzinfo=CN_TZ)       # 周五收盘后
NOON = datetime(2026, 11, 20, 12, 0, tzinfo=CN_TZ)         # 午休
FLOW = {SYM: [{"date": d, "main_net": v} for d, v in
              [("2026-11-13", 315171153.0), ("2026-11-16", 366746452.0), ("2026-11-17", 54397389.0),
               ("2026-11-18", -274158676.0), ("2026-11-19", -132877854.0), ("2026-11-20", 533744622.0)]]}
MARGIN = {SYM: [{"date": d, "financing_balance": v} for d, v in
                [("2026-11-12", 15214545810.0), ("2026-11-13", 15058351439.0), ("2026-11-16", 14788586340.0),
                 ("2026-11-17", 14624879612.0), ("2026-11-18", 14516424905.0), ("2026-11-19", 14489884702.0)]]}
NORTH = [{"date": "2026-06-30", "shares": 53711656.0, "ratio": 0.0429},
         {"date": "2026-09-30", "shares": 55263682.0, "ratio": 0.0442}]


def _lhb(day, listed=True, hot=True):
    items = [{"thscode": SYM, "limit_reason": "日涨幅偏离值达7%", "net_value": -8678850.0}] if listed else []
    seats = [{"name": "某游资", "rows": [{"thscode": SYM, "hot_money_item_net_value": 1.5e7}]}] if hot and listed else []
    return {"state": "ok", "trade_date": day.isoformat(), "source": "meoz", "all": {"stock_items": items},
            "hot_money": {"hot_money_items": seats}, "hot_money_basis": "营业部简称近似匹配"}


@pytest.fixture
def provider(monkeypatch):
    fe._cache.clear()
    p = SimpleNamespace(get_fund_flow=Mock(return_value=FLOW), get_margin_balance=Mock(return_value=MARGIN),
                        get_northbound=Mock(return_value=NORTH),
                        get_fund_flow_now=Mock(return_value={SYM: {"date": "2026-11-20", "main_net": 5.0e8}}))
    monkeypatch.setattr(fe.custom_sources, "names", lambda: {"plugin_x"})
    monkeypatch.setattr(fe.custom_sources, "provider_has_dataset", lambda name, ds: True)
    monkeypatch.setattr(fe.custom_sources, "get_provider", lambda name: p)
    from app.services import dragon_tiger
    monkeypatch.setattr(dragon_tiger, "get_dragon_tiger",
                        lambda data_dir, day: _lhb(day, listed=day == date(2026, 11, 18)))
    return p


def _fund(now, tmp_path):
    return fe.summarize(fe.fetch_holdings([SYM], now, tmp_path), SYM)


def test_window_excludes_today_before_final_cutoff_and_includes_after():
    assert [d.isoformat() for d in fe.window_days(MORNING)] == [
        "2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19"]
    assert fe.window_days(NOON)[-1] == date(2026, 11, 19)
    assert fe.window_days(datetime(2026, 11, 20, 15, 29, tzinfo=CN_TZ))[-1] == date(2026, 11, 19)
    assert fe.window_days(AFTER)[-1] == date(2026, 11, 20) and len(fe.window_days(AFTER)) == 5
    weekend = datetime(2026, 11, 21, 11, 0, tzinfo=CN_TZ)
    assert fe.window_days(weekend)[-1] == date(2026, 11, 20)


def test_intraday_today_row_is_not_counted_in_five_day_total(provider, tmp_path):
    m = _fund(MORNING, tmp_path)["main_net"]
    assert m["status"] == "ok" and m["data_through"] == "2026-11-19"
    assert [d["date"] for d in m["days"]] == ["2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19"]
    assert m["total"] == pytest.approx(315171153 + 366746452 + 54397389 - 274158676 - 132877854)
    assert m["direction"] == "inflow" and m["unit"] == "元" and m["model_basis"] == "vendor_model"
    assert m["source"] == "plugin_x" and m["available_at"] == MORNING.isoformat() and m["missing_days"] == []
    provider.get_fund_flow.assert_called_once_with([SYM], date(2026, 11, 13), date(2026, 11, 19))


def test_after_close_includes_today(provider, tmp_path):
    m = _fund(AFTER, tmp_path)["main_net"]
    assert m["data_through"] == "2026-11-20" and len(m["days"]) == 5


def test_gap_in_flow_is_reported_not_filled_with_zero(provider, tmp_path):
    provider.get_fund_flow.return_value = {SYM: [r for r in FLOW[SYM] if r["date"] != "2026-11-17"]}
    fund = _fund(MORNING, tmp_path)
    assert fund["main_net"]["missing_days"] == ["2026-11-17"] and len(fund["main_net"]["days"]) == 4
    assert "缺 1 日" in fe.evidence_text(fund)


def test_three_states_empty_vs_unavailable(provider, tmp_path):
    provider.get_fund_flow.return_value = {SYM: []}
    provider.get_margin_balance.side_effect = RuntimeError("502")
    provider.get_northbound.side_effect = RuntimeError("down")
    fund = _fund(MORNING, tmp_path)
    assert fund["main_net"]["status"] == "empty" and fund["main_net"]["total"] is None
    assert fund["margin"]["status"] == "unavailable" and fund["northbound"]["status"] == "unavailable"
    text = fe.evidence_text(fund)
    assert "融资余额：资金数据不可用，不能视为无融资" in text
    assert "北向持股：资金数据不可用，不能视为无北向持股" in text
    assert "近 5 个交易日无主力净额记录" in text
    for item in (fund["margin"], fund["northbound"]):
        assert item["available_at"] is None and "balance" not in item and "shares" not in item


def test_no_source_everything_unavailable_never_zero(monkeypatch, tmp_path):
    fe._cache.clear()
    monkeypatch.setattr(fe.custom_sources, "names", lambda: set())
    from app.services import dragon_tiger
    monkeypatch.setattr(dragon_tiger, "get_dragon_tiger", lambda d, day: {"state": "source_unavailable"})
    fund = _fund(MORNING, tmp_path)
    assert all(fund[k]["status"] == "unavailable" for k in ("main_net", "dragon_tiger", "margin", "northbound"))
    text = fe.evidence_text(fund)
    assert text.count("资金数据不可用") == 4 and "0 万元" not in text and "0.00 亿元" not in text
    assert fe.summarize(None, SYM)["main_net"]["status"] == "unavailable"


def test_dragon_tiger_listing_and_hot_money_with_missing_day(provider, tmp_path, monkeypatch):
    from app.services import dragon_tiger

    def fake(data_dir, day):
        if day == date(2026, 11, 19):
            return {"state": "no_data"}
        return _lhb(day, listed=day == date(2026, 11, 18))

    monkeypatch.setattr(dragon_tiger, "get_dragon_tiger", fake)
    fund = _fund(MORNING, tmp_path)
    d = fund["dragon_tiger"]
    assert d["status"] == "ok" and d["missing_days"] == ["2026-11-19"]
    assert [x["date"] for x in d["listings"]] == ["2026-11-18"]
    assert d["hot_money"] == [{"date": "2026-11-18", "name": "某游资", "net_value": 1.5e7}]
    assert d["hot_money_basis"] == "营业部简称近似匹配" and d["data_through"] == "2026-11-18"
    assert "游资席位 某游资" in fe.evidence_text(fund)


def test_dragon_fallback_prev_is_not_taken_as_that_day(provider, tmp_path, monkeypatch):
    from app.services import dragon_tiger
    monkeypatch.setattr(dragon_tiger, "get_dragon_tiger",
                        lambda data_dir, day: {**_lhb(date(2026, 11, 18)), "state": "fallback_prev"})
    assert _fund(MORNING, tmp_path)["dragon_tiger"]["status"] == "unavailable"


def test_dragon_not_listed_is_empty_not_unavailable(provider, tmp_path, monkeypatch):
    from app.services import dragon_tiger
    monkeypatch.setattr(dragon_tiger, "get_dragon_tiger", lambda data_dir, day: _lhb(day, listed=False))
    d = _fund(MORNING, tmp_path)["dragon_tiger"]
    assert d["status"] == "empty" and d["listings"] == [] and d["missing_days"] == []


def test_margin_change_against_fifth_previous_and_t_minus_1(provider, tmp_path):
    fund = _fund(AFTER, tmp_path)
    g = fund["margin"]
    assert g["status"] == "ok" and g["data_through"] == "2026-11-19"  # 收盘后仍是 T-1
    assert g["balance"] == 14489884702.0 and g["compare_date"] == "2026-11-12"
    assert g["change"] == pytest.approx(14489884702 - 15214545810)
    assert g["change_pct"] == pytest.approx(g["change"] / 15214545810)
    assert "T-1" in fe.evidence_text(fund)


def test_margin_insufficient_history_and_non_margin(provider, tmp_path):
    provider.get_margin_balance.return_value = {SYM: MARGIN[SYM][-3:]}
    g = _fund(MORNING, tmp_path)["margin"]
    assert g["status"] == "ok" and g["change"] is None and g["compare_date"] is None
    fe._cache.clear()
    provider.get_margin_balance.return_value = {SYM: []}
    assert _fund(MORNING, tmp_path)["margin"]["status"] == "empty"


def test_northbound_quarterly_change_and_twelve_hour_cache(provider, tmp_path):
    fund = _fund(MORNING, tmp_path)
    n = fund["northbound"]
    assert n["status"] == "ok" and n["data_through"] == "2026-09-30" and n["disclosure"] == "quarterly"
    assert n["shares_change"] == pytest.approx(55263682 - 53711656)
    assert n["ratio_change"] == pytest.approx(0.0013)
    text = fe.evidence_text(fund)
    assert "占 A 股 4.42%" in text and "+0.13 个百分点" in text and "季度披露" in text
    fe._cache = {k: v for k, v in fe._cache.items() if k[0] == "north"}  # 持仓缓存过期, 北向仍在缓存期
    fe.fetch_holdings([SYM], MORNING, tmp_path)
    assert provider.get_northbound.call_count == 1


def test_holdings_cache_hit_makes_no_second_request(provider, tmp_path):
    fe.fetch_holdings([SYM], MORNING, tmp_path)
    fe.fetch_holdings([SYM], MORNING, tmp_path)
    assert provider.get_fund_flow.call_count == 1 and provider.get_margin_balance.call_count == 1


def test_evidence_text_states_vendor_model_and_no_intent(provider, tmp_path):
    text = fe.evidence_text(_fund(MORNING, tmp_path))
    assert "厂商模型口径" in text and "不代表主力意图" in text
    assert "主力净额合计 +3.29 亿元（净流入，截至 11-19）" in text
    assert "上龙虎榜 1 次（11-18）" in text and "融资余额 144.90 亿元" in text


# ---- 做T: 当日累计 ----

def test_intraday_ok_cached_four_minutes_and_stale_after_ten(provider, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(fe.time, "monotonic", lambda: clock["t"])
    out = fe.intraday_main_net([SYM], MORNING)[SYM]
    assert out["status"] == "ok" and out["main_net"] == 5.0e8 and out["kind"] == "intraday_cumulative"
    assert out["available_at"] == MORNING.isoformat() and out["data_through"] == "2026-11-20"
    assert "4 分钟" in out["refresh_hint"] and out["model_basis"] == "vendor_model"
    clock["t"] += 100
    fe.intraday_main_net([SYM], MORNING)
    assert provider.get_fund_flow_now.call_count == 1
    clock["t"] += 200  # 超过 4 分钟: 重新取
    later = datetime(2026, 11, 20, 10, 5, tzinfo=CN_TZ)
    assert fe.intraday_main_net([SYM], later)[SYM]["available_at"] == later.isoformat()
    assert provider.get_fund_flow_now.call_count == 2
    # 之后取数失败: 10 分钟内沿用旧值 (取数时刻不前移), 之后不可用
    provider.get_fund_flow_now.side_effect = RuntimeError("502")
    clock["t"] += 300
    kept = fe.intraday_main_net([SYM], datetime(2026, 11, 20, 10, 10, tzinfo=CN_TZ))[SYM]
    assert kept["status"] == "ok" and kept["available_at"] == later.isoformat()
    clock["t"] += 400
    assert fe.intraday_main_net([SYM], datetime(2026, 11, 20, 10, 17, tzinfo=CN_TZ))[SYM]["status"] == "unavailable"


def test_intraday_not_today_missing_and_no_source(provider, monkeypatch):
    provider.get_fund_flow_now.return_value = {SYM: {"date": "2026-11-19", "main_net": 1.0}}
    assert fe.intraday_main_net([SYM], MORNING)[SYM]["status"] == "unavailable"
    fe._cache.clear()
    provider.get_fund_flow_now.return_value = {}
    assert fe.intraday_main_net([SYM], MORNING)[SYM]["status"] == "empty"
    fe._cache.clear()
    monkeypatch.setattr(fe.custom_sources, "names", lambda: set())
    assert fe.intraday_main_net([SYM], MORNING)[SYM]["status"] == "unavailable"
    assert fe.intraday_main_net([], MORNING) == {}


# ---- 回放可见性 ----

def test_visible_at_hides_items_not_yet_available(provider, tmp_path):
    fund = _fund(MORNING, tmp_path)
    early = datetime(2026, 11, 20, 9, 0, tzinfo=CN_TZ)
    hidden = fe.visible_at(fund, early)
    assert all(hidden[k]["status"] == "unavailable" for k in ("main_net", "dragon_tiger", "margin", "northbound"))
    assert "total" not in hidden["main_net"]
    assert fe.visible_at(fund, MORNING)["main_net"]["total"] == fund["main_net"]["total"]
    single = fe.intraday_main_net([SYM], MORNING)[SYM]
    assert fe.visible_at(single, early)["status"] == "unavailable"
    assert fe.visible_at(single, MORNING)["main_net"] == 5.0e8
    assert fe.visible_at(None, MORNING) is None
