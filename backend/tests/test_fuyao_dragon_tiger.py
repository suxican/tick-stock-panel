"""fuyao 龙虎榜单日三榜接口: 三榜聚合, 「无数据」(0 条 / 1002 / 1003) 与真失败分开。"""

from __future__ import annotations

from datetime import date

import pytest

from app.plugins.fuyao.client import FuyaoError
from app.plugins.fuyao.provider import FuyaoProvider

DAY = date(2026, 11, 18)


class _Client:
    def __init__(self, *, errors: dict | None = None, empty: bool = False):
        self.errors = errors or {}
        self.empty = empty
        self.calls: list[tuple[str, str | None]] = []

    def dragon_tiger_list(self, board_type="all", date=None):
        self.calls.append((board_type, date))
        if board_type in self.errors:
            raise self.errors[board_type]
        items = [] if self.empty else [{"thscode": "600519.SH", "name": "贵州茅台", "net_value": 1e8}]
        if board_type == "hot_money":
            return {"trade_date": date, "count": 1,
                    "hot_money_items": [] if self.empty else [{"name": "佛山系", "buying": 2e8, "rows": items}]}
        return {"trade_date": date, "count": len(items), "stock_count": len(items), "stock_items": items}


def _provider(monkeypatch, client) -> FuyaoProvider:
    provider = FuyaoProvider()
    monkeypatch.setattr(provider, "_get_client", lambda: client)
    return provider


def test_fuyao_returns_three_boards(monkeypatch):
    client = _Client()
    out = _provider(monkeypatch, client).get_dragon_tiger(DAY)
    assert client.calls == [("all", "2026-11-18"), ("org", "2026-11-18"), ("hot_money", "2026-11-18")]
    assert out["trade_date"] == "2026-11-18"
    assert out["all"]["stock_items"][0]["thscode"] == "600519.SH"
    assert out["org"]["stock_count"] == 1
    assert out["hot_money"]["hot_money_items"][0]["name"] == "佛山系"
    assert "hot_money_basis" not in out


def test_fuyao_empty_all_board_returns_none(monkeypatch):
    client = _Client(empty=True)
    assert _provider(monkeypatch, client).get_dragon_tiger(DAY) is None
    assert client.calls == [("all", "2026-11-18")]          # 总榜为空即止, 不再拉另两榜


@pytest.mark.parametrize("code", [1002, "1003"])
def test_fuyao_no_data_codes_return_none(monkeypatch, code):
    client = _Client(errors={"all": FuyaoError("x", code=code)})
    assert _provider(monkeypatch, client).get_dragon_tiger(DAY) is None


def test_fuyao_non_trading_day_returns_none(monkeypatch):
    client = _Client(errors={"all": FuyaoError("非交易日", code=1002)})
    assert _provider(monkeypatch, client).get_dragon_tiger(DAY) is None


def test_fuyao_out_of_range_returns_none(monkeypatch):
    client = _Client(errors={"all": FuyaoError("date must be within one year", code=1003)})
    assert _provider(monkeypatch, client).get_dragon_tiger(DAY) is None


def test_fuyao_other_error_raises(monkeypatch):
    client = _Client(errors={"all": FuyaoError("server", code=5000)})
    with pytest.raises(FuyaoError):
        _provider(monkeypatch, client).get_dragon_tiger(DAY)


def test_fuyao_org_failure_fails_whole_day(monkeypatch):
    client = _Client(errors={"org": FuyaoError("server", code=5000)})
    with pytest.raises(FuyaoError):
        _provider(monkeypatch, client).get_dragon_tiger(DAY)
