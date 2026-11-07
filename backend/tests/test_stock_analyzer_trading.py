"""个股操作建议的数据边界与实际流式提示词契约。"""
from __future__ import annotations

import json
from datetime import date

import polars as pl
import pytest

from app.services import stock_analyzer


def test_financial_context_includes_cash_and_debt_with_bounded_periods(monkeypatch, tmp_path):
    calls = []
    fields = {"metrics": "roe", "income": "net_income",
              "balance_sheet": "total_liabilities", "cash_flow": "net_operating_cash_flow"}

    def financials(_data_dir, table):
        calls.append(table)
        return pl.DataFrame({
            "symbol": ["600000.SH"] * 5 + ["600001.SH"],
            "period_end": [date(2025, month, 28) for month in range(1, 6)] + [date(2026, 1, 1)],
            fields[table]: [1.0, 2.0, 3.0, float("nan"), 5.0, 999.0],
            "announce_date": [date(2025, 6, 1)] * 6,
            "unrelated_vendor_field": ["omitted"] * 6,
        })

    monkeypatch.setattr(stock_analyzer, "get_financial_df", financials)
    result = stock_analyzer._load_financials(tmp_path, "600000.SH")

    assert set(calls) == {"metrics", "income", "balance_sheet", "cash_flow"}
    for table, records in result.items():
        assert len(records) == 4
        assert records[0]["period_end"] == "2025-05-28"
        assert records[0]["announce_date"] == "2025-06-01"
        assert records[1][fields[table]] is None
        assert all("symbol" not in row and row[fields[table]] != 999 for row in records)
        assert all("unrelated_vendor_field" not in row for row in records)
    json.dumps(result, allow_nan=False)


def test_financial_context_missing_table_and_period_is_bounded(monkeypatch, tmp_path):
    def financials(_data_dir, table):
        if table != "metrics":
            return pl.DataFrame()
        return pl.DataFrame({"symbol": ["600000.SH"] * 8, "roe": list(range(8))})

    monkeypatch.setattr(stock_analyzer, "get_financial_df", financials)
    result = stock_analyzer._load_financials(tmp_path, "600000.SH")
    assert len(result["metrics"]) <= 4
    assert result["cash_flow"] == []
    assert result["balance_sheet"] == []


def test_prompt_identifies_actual_window_missing_evidence_and_price_basis():
    prompt = stock_analyzer._build_user_prompt(
        [{"date": "2026-10-30", "close": 10.0, "amount": 1000000}],
        {}, {}, 10.0, "600000.SH", "买入区间和止损价", raw_close=20.0,
    )
    assert "行情截止交易日: 2026-10-30" in prompt
    assert "实际 1 根日 K" in prompt
    assert "前复权" in prompt and "不复权" in prompt
    assert "主力/北向/龙虎榜资金流" in prompt
    assert "新闻/公告/研报" in prompt
    assert "未提供" in prompt
    assert "不得给出相应操作结论" not in prompt


def test_execution_levels_use_raw_scale_without_mutating_chart_levels():
    levels = {"sr": [{"value": 9.0, "label": "支撑"}, {"value": 11.0, "label": "压力"}]}
    original = json.dumps(levels)
    part = stock_analyzer._execution_price_part(levels, close=10.0, raw_close=20.0)
    assert "支撑折算=18.0" in part
    assert "压力折算=22.0" in part
    assert "raw_close/close=2.00000000" in part
    assert json.dumps(levels) == original


@pytest.mark.parametrize("price", [None, 0.0, float("nan"), float("inf")])
def test_execution_levels_fail_closed_without_valid_prices(price):
    assert "待核实" in stock_analyzer._execution_price_part({}, close=10.0, raw_close=price)
    assert "待核实" in stock_analyzer._execution_price_part({}, close=price, raw_close=10.0)


def test_paper_position_does_not_mix_adjusted_price_with_raw_cost():
    part = stock_analyzer._paper_position_part(
        [{"qty": 100, "avg_cost": 20.0}], close=10.0, raw_close=None,
    )
    assert "浮动盈亏待核实" in part
    assert "-50.00%" not in part


@pytest.mark.asyncio
@pytest.mark.parametrize("asset_type, raw_close", [
    ("stock", 10.0), ("stock", None), ("etf", 10.0), ("index", 10.0),
])
async def test_stream_sends_actionable_framework_and_available_data(
    monkeypatch, tmp_path, asset_type, raw_close,
):
    from app.services import ai_provider
    from app.strategy import paper

    frame = pl.DataFrame({
        "date": [date(2026, 10, 30)], "close": [10.0], "raw_close": [raw_close],
        "amount": [1000000.0], "turnover_rate": [3.0],
    })
    monkeypatch.setattr(stock_analyzer, "_load_kline", lambda *_: frame)
    monkeypatch.setattr(stock_analyzer, "compute_levels", lambda _: {})
    def financials(*_):
        assert asset_type == "stock", "ETF/指数不读取公司财务数据"
        return {"metrics": []}

    monkeypatch.setattr(stock_analyzer, "_load_financials", financials)
    monkeypatch.setattr(paper, "list_account_ids", lambda _: [])
    captured = {}

    async def stream(messages, **kwargs):
        captured["messages"] = messages
        yield "测试报告"

    monkeypatch.setattr(ai_provider, "stream_ai_text", stream)

    class Repo:
        def resolve_asset_type(self, _symbol):
            return asset_type

        def get_name_map(self, _symbols):
            return {"600000.SH": "测试股票"}

    events = [json.loads(event) async for event in stock_analyzer.analyze_stock_stream(
        Repo(), tmp_path, "600000.SH", "现在能买吗,止损多少",
    )]
    assert [event["type"] for event in events] == ["meta", "delta", "done"]
    system, user = (message["content"] for message in captured["messages"])
    assert all(action in system for action in ("买入", "持有", "减仓", "止损", "止盈"))
    assert "操作计划" in system and "失效与退出条件" in system
    assert "N/A" in system and "不得折算" in system
    assert "不得编造上涨/下跌概率" in system
    assert "不包含任何买卖或操作建议" not in system
    assert "不得给出相应操作结论" not in user
    assert '"amount": 1000000.0' in user
    assert "标的名称: 测试股票" in user
    if raw_close is None:
        assert "不复权委托参考价位: 待核实" in user
    if asset_type != "stock":
        assert "公司财务/业绩评分不适用" in user


@pytest.mark.asyncio
@pytest.mark.parametrize("closes", [[], [None], [0.0], [float("nan")], [float("inf")]])
async def test_unusable_kline_does_not_request_trade_advice(monkeypatch, tmp_path, closes):
    from app.services import ai_provider

    monkeypatch.setattr(stock_analyzer, "_load_kline", lambda *_: pl.DataFrame({"close": closes}))

    async def fail(*args, **kwargs):
        pytest.fail("没有行情不应调用模型生成操作价位")
        yield ""

    monkeypatch.setattr(ai_provider, "stream_ai_text", fail)
    events = [json.loads(event) async for event in stock_analyzer.analyze_stock_stream(
        object(), tmp_path, "600000.SH",
    )]
    assert len(events) == 1
    assert events[0]["type"] == "error"
