"""猫爪资金证据 provider 契约: 主力净额 / 两融余额 / 北向持股, 转为源中立结构 (单位、日期、代码)。"""
from __future__ import annotations

from datetime import date

import pytest

from app.plugins.meoz import client as mc
from tests.test_meoz_provider import _provider

DAY = date(2026, 11, 20)


def test_fund_flow_range_uses_live_service_and_normalizes(monkeypatch):
    calls = []

    def handler(apiname, params, fields, service):
        calls.append((apiname, dict(params), service))
        return [
            {"tradedate": "20261119", "symbol": "600519", "main_net_amount": -132877854},
            {"tradedate": "20261118", "symbol": "600519", "main_net_amount": -274158676},
            {"tradedate": "20261118", "symbol": "000001", "main_net_amount": None},
            {"tradedate": "20261118", "symbol": "999999", "main_net_amount": 1},  # 非请求代码: 忽略
        ]

    provider, _ = _provider(monkeypatch, handler)
    out = provider.get_fund_flow(["600519.SH", "000001.SZ", "300750.SZ"], date(2026, 11, 13), DAY)
    assert out["600519.SH"] == [{"date": "2026-11-18", "main_net": -274158676.0},
                                {"date": "2026-11-19", "main_net": -132877854.0}]
    assert out["000001.SZ"] == []  # 空值行被跳过, 不补 0
    assert out["300750.SZ"] == []
    assert calls == [("fundflow_kp", {"symbols": "000001,300750,600519", "startdate": "20261113",
                                      "enddate": "20261120"}, "live")]


def test_fund_flow_now_reads_screening_current_cumulative(monkeypatch):
    seen = []

    def handler(apiname, params, fields, service):
        seen.append((apiname, dict(params), fields, service))
        return [{"symbol": "600519", "tradedate": "20261120", "main_net_amount": 533744622},
                {"symbol": "000001", "tradedate": "20261120", "main_net_amount": None}]

    provider, _ = _provider(monkeypatch, handler)
    out = provider.get_fund_flow_now(["600519.SH", "000001.SZ"])
    assert out == {"600519.SH": {"date": "2026-11-20", "main_net": 533744622.0}}
    assert seen == [("screening", {"symbols": "000001,600519"}, "symbol,tradedate,main_net_amount", "live")]


def test_margin_balance_history_service_and_sorting(monkeypatch):
    calls = []

    def handler(apiname, params, fields, service):
        calls.append((apiname, dict(params), service))
        return [{"symbol": "600519", "tradedate": "20261119", "financing_balance": 14489884702},
                {"symbol": "600519", "tradedate": "20261112", "financing_balance": 15214545810},
                {"symbol": "600519", "tradedate": "20261113", "financing_balance": None}]

    provider, _ = _provider(monkeypatch, handler)
    out = provider.get_margin_balance(["600519.SH", "688001.SH"], date(2026, 10, 1), DAY)
    assert out["600519.SH"] == [{"date": "2026-11-12", "financing_balance": 15214545810.0},
                                {"date": "2026-11-19", "financing_balance": 14489884702.0}]
    assert out["688001.SH"] == []
    assert calls[0][0] == "margin_detail" and calls[0][2] == "history"
    assert calls[0][1] == {"symbols": "600519,688001", "startdate": "20261001", "enddate": "20261120"}


def test_northbound_queries_by_six_digit_code_and_returns_latest_two(monkeypatch):
    calls = []

    def handler(apiname, params, fields, service):
        calls.append((apiname, dict(params), service))
        # 厂商返回港交所中央结算编码 (90519), 不得被当作代码使用
        return [{"symbol": "90519", "trade_date": "20260630", "vol": 53711656, "ratio": 4.29},
                {"symbol": "90519", "trade_date": "20260930", "vol": 55263682, "ratio": 4.42},
                {"symbol": "90519", "trade_date": "20260331", "vol": 58733069, "ratio": 4.69}]

    provider, _ = _provider(monkeypatch, handler)
    out = provider.get_northbound("600519.SH", DAY)
    assert out == [{"date": "2026-06-30", "shares": 53711656.0, "ratio": pytest.approx(0.0429)},
                   {"date": "2026-09-30", "shares": 55263682.0, "ratio": pytest.approx(0.0442)}]
    assert calls[0][0] == "northbound_holding" and calls[0][2] == "history"
    assert calls[0][1]["symbols"] == "600519" and calls[0][1]["enddate"] == "20261120"


def test_northbound_empty_rows_mean_no_record(monkeypatch):
    provider, _ = _provider(monkeypatch, lambda *a: [])
    assert provider.get_northbound("300001.SZ", DAY) == []


@pytest.mark.parametrize("method,args", [
    ("get_fund_flow", (["600519.SH"], date(2026, 11, 13), DAY)),
    ("get_fund_flow_now", (["600519.SH"],)),
    ("get_margin_balance", (["600519.SH"], date(2026, 11, 13), DAY)),
    ("get_northbound", ("600519.SH", DAY)),
])
def test_upstream_error_raises_so_caller_marks_unavailable(monkeypatch, method, args):
    def boom(*a):
        raise mc.MeozError("down")

    provider, _ = _provider(monkeypatch, boom)
    with pytest.raises(mc.MeozError):
        getattr(provider, method)(*args)


def test_empty_symbol_list_makes_no_request(monkeypatch):
    def boom(*a):
        raise AssertionError("不应发请求")

    provider, _ = _provider(monkeypatch, boom)
    assert provider.get_fund_flow([], DAY, DAY) == {}
    assert provider.get_fund_flow_now([]) == {}
    assert provider.get_margin_balance([], DAY, DAY) == {}
