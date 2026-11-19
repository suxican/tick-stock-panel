"""猫爪龙虎榜适配 (longhubang_stock / longhubang_seat / longhubang_hot_money → 三榜契约)。

样本按 2026-11-16 实测形状构造: 百分数口径、同一股票多条上榜原因多行、席位全称、
游资名单为营业部简称 JSON 字符串。不触网。
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.plugins.meoz import client as mc
from app.plugins.meoz import provider as mp
from app.plugins.meoz.provider import MeozProvider

DAY = date(2026, 11, 16)
YMD = "20261116"
R1 = "日涨幅偏离值达到7%的前5只证券"
R1B = "日换手率达到20%的前5只证券"
R3 = "连续三个交易日内，涨幅偏离值累计达到20%的证券"


def _stock(symbol, name, *, reason=R1, net=1.0e7, rate=1.5, pct=10.0, amount=5.0e8, buy=3.0e7, sell=2.0e7, ymd=YMD):
    return {"tradedate": ymd, "symbol": symbol, "name": name, "close": 10.0, "pct_chg": pct,
            "turnover_rate": 5.0, "amount": amount, "lhb_buy": buy, "lhb_sell": sell,
            "lhb_amount": buy + sell, "net_amount": net, "net_rate": rate, "amount_rate": 10.0,
            "float_value": 1e10, "reason": reason}


def _seat(symbol, seat_name, *, side=0, net=1.0e6, reason=R1, ymd=YMD):
    buy, sell = (max(net, 0), max(-net, 0))
    return {"tradedate": ymd, "symbol": symbol, "name": "x", "reason": reason, "side": side,
            "seat_name": seat_name, "buy": buy, "buy_rate": 1.0, "sell": sell, "sell_rate": 0.0, "net_buy": net}


HOT_MONEY = [
    {"name": "华泰雄州", "description": "", "seats": json.dumps(["华泰证券南京六合雄州西路"], ensure_ascii=False)},
    {"name": "宁波桑田路", "description": "", "seats": json.dumps(["国盛证券宁波桑田路"], ensure_ascii=False)},
    {"name": "机构专用", "description": "", "seats": json.dumps(["机构专用"], ensure_ascii=False)},
    {"name": "沪股通专用", "description": "", "seats": json.dumps(["沪股通专用"], ensure_ascii=False)},
]


class _Fake:
    def __init__(self, data):
        self.data = data
        self.calls: list[tuple[str, dict, str]] = []

    def query(self, apiname, params=None, fields=None, *, service="live"):
        self.calls.append((apiname, dict(params or {}), service))
        if apiname == "stockbasic":
            # 股票池不含 B 股与部分代码: 验证后缀先查股票池、再按代码段推断
            return [{"code": "600519.SH", "symbol": "600519"}, {"code": "920001.BJ", "symbol": "920001"}]
        value = self.data.get(apiname, [])
        return value(params) if callable(value) else value

    def close(self):
        pass


@pytest.fixture
def make(monkeypatch):
    def _make(stock, seat, hot_money=HOT_MONEY):
        fake = _Fake({"longhubang_stock": stock, "longhubang_seat": seat, "longhubang_hot_money": hot_money})
        monkeypatch.setattr(mp.meoz_client, "MeozClient", lambda **kw: fake)
        monkeypatch.setattr(mp, "get_api_key", lambda: "k-test")
        return MeozProvider(), fake
    return _make


def _row(board, code, range_days=1):
    return next(i for i in board["stock_items"] if i["thscode"] == code and i["range_days"] == range_days)


def test_all_board_converts_percent_and_dedupes_reasons(make):
    reasons = [R1, R1B, "日振幅值达到15%的前5只证券"]
    stock = [_stock("002086", "东方海洋", reason=r, net=18826659.38, rate=0.79, pct=-9.9585) for r in reasons]
    provider, _ = make(stock, [])
    out = provider.get_dragon_tiger(DAY)
    assert out["trade_date"] == "2026-11-16"
    items = out["all"]["stock_items"]
    assert len(items) == 1
    item = items[0]
    assert item["thscode"] == "002086.SZ" and item["ticker"] == "002086" and item["range_days"] == 1
    assert item["net_value"] == 18826659.38
    assert item["net_rate"] == pytest.approx(0.0079)
    assert item["change"] == pytest.approx(-0.099585)
    assert (item["buy_value"], item["sell_value"]) == (3.0e7, 2.0e7)
    assert item["hot_rank"] is None and item["concept_list"] is None
    assert item["limit_reason"] == "；".join(reasons)


def test_three_day_reason_becomes_range_days_3(make):
    stock = [_stock("002148", "北纬科技", reason=R1, net=-88290941.65),
             _stock("002148", "北纬科技", reason=R1B, net=-88290941.65),
             _stock("002148", "北纬科技", reason=R3, net=31669791.35)]
    provider, _ = make(stock, [])
    board = provider.get_dragon_tiger(DAY)["all"]
    assert {(i["range_days"], i["net_value"]) for i in board["stock_items"]} == {(1, -88290941.65), (3, 31669791.35)}


def test_stock_counts(make):
    stock = [_stock("002148", "a", reason=R1), _stock("002148", "a", reason=R3), _stock("600715", "b")]
    provider, _ = make(stock, [])
    board = provider.get_dragon_tiger(DAY)["all"]
    assert (board["stock_count"], board["count"]) == (2, 3)


def test_org_board_aggregates_institution_seats(make):
    stock = [_stock("002463", "沪电股份", net=4.57e8, amount=6.444e9)]
    seat = [_seat("002463", "机构专用", side=0, net=2.0e8),
            _seat("002463", "机构专用", side=0, net=1.0e8),
            _seat("002463", "机构专用", side=1, net=-0.5e8),
            _seat("002463", "华泰证券股份有限公司南京六合雄州西路证券营业部", side=0, net=3e7)]
    provider, _ = make(stock, seat)
    org = provider.get_dragon_tiger(DAY)["org"]
    assert len(org["stock_items"]) == 1
    item = _row(org, "002463.SZ")
    assert item["org_net_value"] == pytest.approx(2.5e8)
    assert (item["org_buy_num"], item["org_sell_num"]) == (2, 1)
    assert item["org_net_rate"] == pytest.approx(2.5e8 / 6.444e9)
    assert item["net_value"] == 4.57e8          # 其余字段来自总榜同键
    assert org["stock_count"] == 1


def test_org_seat_in_both_buy_and_sell_lists_counted_once_in_net(make):
    # 实测 11-16: 同一机构同时进买入前五 (side 0) 与卖出前五 (side 1), 两行金额相同
    stock = [_stock("300824", "x", amount=2.75e8)]
    both = {"buy": 3e6, "sell": 9e6, "net_buy": -6e6}
    seat = [{**_seat("300824", "机构专用", side=0), **both},
            {**_seat("300824", "机构专用", side=1), **both},
            _seat("300824", "机构专用", side=1, net=-2e6)]
    provider, _ = make(stock, seat)
    item = _row(provider.get_dragon_tiger(DAY)["org"], "300824.SZ")
    assert item["org_net_value"] == pytest.approx(-8e6)
    assert (item["org_buy_num"], item["org_sell_num"]) == (1, 2)   # 家数仍按所在榜单方向计


def test_org_seat_rows_deduped_across_reasons(make):
    stock = [_stock("002463", "x", reason=R1), _stock("002463", "x", reason=R1B)]
    seat = [_seat("002463", "机构专用", side=0, net=1e8, reason=R1),
            _seat("002463", "机构专用", side=0, net=1e8, reason=R1B)]   # 同一席位、两条原因
    provider, _ = make(stock, seat)
    assert _row(provider.get_dragon_tiger(DAY)["org"], "002463.SZ")["org_net_value"] == pytest.approx(1e8)


def test_hot_money_matches_normalized_seat_names_and_excludes_channels(make):
    stock = [_stock("002384", "东山精密"), _stock("000678", "襄阳轴承")]
    seat = [_seat("002384", "华泰证券股份有限公司南京六合雄州西路证券营业部", net=2.13e8),
            _seat("000678", "国盛证券有限责任公司宁波桑田路证券营业部", side=1, net=-1.18e7),
            _seat("000678", "机构专用", net=5e6),
            _seat("002384", "沪股通专用", net=7e6)]
    provider, _ = make(stock, seat)
    out = provider.get_dragon_tiger(DAY)
    seats = out["hot_money"]["hot_money_items"]
    assert [s["name"] for s in seats] == ["华泰雄州", "宁波桑田路"]           # 按 buying 降序, 不含通道/机构
    assert seats[0]["buying"] == pytest.approx(2.13e8)
    row = seats[0]["rows"][0]
    assert row["thscode"] == "002384.SZ" and row["hot_money_item_net_value"] == pytest.approx(2.13e8)
    assert row["name"] == "东山精密"
    assert out["hot_money"]["count"] == 2
    assert out["hot_money_basis"] == "meoz_seat_match"


def test_hot_money_counts_seat_once_across_ranges(make):
    stock = [_stock("002148", "a", reason=R1), _stock("002148", "a", reason=R3)]
    name = "华泰证券股份有限公司南京六合雄州西路证券营业部"
    seat = [_seat("002148", name, net=5e6, reason=R1), _seat("002148", name, net=9e6, reason=R3)]
    provider, _ = make(stock, seat)
    hm = provider.get_dragon_tiger(DAY)["hot_money"]["hot_money_items"]
    assert hm[0]["buying"] == pytest.approx(5e6)                              # 只计 1 日榜那条
    assert len(hm[0]["rows"]) == 1


def test_symbols_get_suffix_for_b_share_and_bse(make):
    stock = [_stock("200056", "皇庭B"), _stock("920021", "某北交所"), _stock("600715", "文投控股")]
    provider, _ = make(stock, [])
    codes = {i["thscode"] for i in provider.get_dragon_tiger(DAY)["all"]["stock_items"]}
    assert codes == {"200056.SZ", "920021.BJ", "600715.SH"}


def test_empty_stock_rows_return_none(make):
    provider, fake = make([], [])
    assert provider.get_dragon_tiger(DAY) is None
    assert [c[0] for c in fake.calls if c[0].startswith("longhubang")] == ["longhubang_stock"]


def test_trade_date_mismatch_raises(make):
    provider, _ = make([_stock("600715", "x", ymd="20261113")], [])
    with pytest.raises(mc.MeozError):
        provider.get_dragon_tiger(DAY)


def test_queries_history_service_with_trade_date(make):
    provider, fake = make([_stock("600715", "x")], [])
    provider.get_dragon_tiger(DAY)
    lhb = [c for c in fake.calls if c[0].startswith("longhubang")]
    assert ("longhubang_stock", {"tradedate": YMD}, "history") in lhb
    assert ("longhubang_seat", {"tradedate": YMD}, "history") in lhb


def test_hot_money_list_cached_between_days(make):
    provider, fake = make([_stock("600715", "x")], [])
    provider.get_dragon_tiger(DAY)
    provider.get_dragon_tiger(DAY)
    assert sum(1 for c in fake.calls if c[0] == "longhubang_hot_money") == 1
