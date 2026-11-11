"""复盘持仓联动: 批次持仓 × 盘面, 程序条件, 只看市场联动降级。"""
from datetime import date
from types import SimpleNamespace

import polars as pl

from app.services import market_recap
from app.services import market_recap_holdings as mh
from app.services.market_mainline import mainline_path


def _engine_row(**over):
    row = {
        "symbol": "600522.SH", "name": "中天科技", "asset_type": "stock",
        "qty": 400, "cost_price": 58.0,
        "opened_on": None, "profile": {"invalidation": ""},
        "pnl_ratio": -0.575, "pnl_state": "deep_loss", "age_days": None, "trend": "down",
        "relative_strength": "weaker",
        "price": {"value": 24.65, "date": "2026-11-11"},
        "technicals": {"support": 22.1, "resistance": 26.8},
        "sectors": [
            {"key": "i1", "name": "通信设备 / 通信线缆及配套", "kind": "industry", "valid": True, "change_ratio": 0.0625},
            {"key": "i0", "name": "通信设备", "kind": "industry", "valid": True, "change_ratio": 0.05},
            {"key": "c1", "name": "光纤概念", "kind": "concept", "valid": True, "change_ratio": 0.044},
            {"key": "c2", "name": "特斯拉概念", "kind": "concept", "valid": True, "change_ratio": 0.03},
        ],
        "plan": {"can_adopt": False, "scenarios": []},
        "warnings": ["实时行情未启用或已暂停，仅使用完整日线", "请先填写研究失效条件"],
    }
    row.update(over)
    return row


SNAPSHOT = {
    "as_of": "2026-11-11",
    "intraday": False,
    "sectors": {
        "leading": [{"name": "通信线缆及配套", "pct": 6.25}, {"name": "光纤概念", "pct": 4.42}],
        "lagging": [{"name": "白酒", "pct": -3.08}],
    },
}


# ---------------------------------------------------------------- 模式与条件
def test_missing_basics_falls_back_to_market_only_with_observation_conditions():
    row = mh._row(_engine_row(), mh._sector_moves(SNAPSHOT), {"特斯拉概念": {"rank": 1, "streak": 3}})
    assert row["mode"] == "market"
    assert row["missing"] == ["建仓日", "失效条件"]
    assert row["age_days"] is None
    # 只描述联动, 不给仓位动作
    assert [c["label"] for c in row["conditions"]] == ["收盘 ≥ 26.80(压力)", "收盘 ≤ 22.10(支撑)"]
    assert all("减仓" not in c["meaning"] and "止盈" not in c["meaning"] for c in row["conditions"])
    # 联动: 最细行业 + 当日涨幅前列 + 涨停主线
    assert row["sector"] == {"name": "通信线缆及配套", "pct": 6.25}
    assert {h["name"] for h in row["hot"]} == {"通信线缆及配套", "光纤概念"}
    assert row["mainline"] == {"name": "特斯拉概念", "rank": 1, "streak": 3}
    # 收盘后复盘只用日线属预期行为, 不当作告警
    assert row["warnings"] == ["请先填写研究失效条件"]


def test_full_mode_reuses_engine_scenario_actions_and_risk_price():
    engine = _engine_row(
        opened_on="2026-10-20", profile={"invalidation": "跌破年线"}, age_days=15,
        plan={"can_adopt": True, "scenarios": [
            {"key": "base", "action": "保持观察", "price_trigger": None},
            {"key": "bull", "action": "确认反弹力度后评估分步减仓", "price_trigger": {"op": ">=", "value": 26.8}},
            {"key": "bear", "action": "触及研究风控价后评估减仓或退出", "price_trigger": {"op": "<=", "value": 22.5}},
        ]},
    )
    row = mh._row(engine, {}, {})
    assert row["mode"] == "full" and row["missing"] == [] and row["age_days"] == 15
    assert [(c["label"], c["meaning"]) for c in row["conditions"]] == [
        ("收盘 ≥ 26.80", "确认反弹力度后评估分步减仓"),
        ("收盘 ≤ 22.50", "触及研究风控价后评估减仓或退出"),
    ]


def test_full_basics_but_engine_not_ready_stays_market_only():
    row = mh._row(_engine_row(opened_on="2026-10-20", profile={"invalidation": "x"}), {}, {})
    assert row["mode"] == "market" and row["missing"] == []


def test_no_technicals_means_no_conditions():
    row = mh._row(_engine_row(technicals={"support": None, "resistance": None}), {}, {})
    assert row["conditions"] == []


# ---------------------------------------------------------------- 主线名次
def test_mainline_ranks_requires_as_of_and_counts_streak(tmp_path):
    days = [date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11)]
    rows = [{"date": d, "kind": "concept", "member": "特斯拉概念", "rank": r, "limit_up_count": 5,
             "max_boards": 2, "leader_symbol": None, "score": 1.0} for d, r in zip(days, [20, 3, 1])]
    rows.append({"date": days[-1], "kind": "concept", "member": "无关", "rank": 2, "limit_up_count": 1,
                 "max_boards": 1, "leader_symbol": None, "score": 1.0})
    p = mainline_path(tmp_path)
    p.parent.mkdir(parents=True)
    pl.DataFrame(rows).write_parquet(p)

    got = mh._mainline_ranks(tmp_path, days[-1], {"特斯拉概念", "光纤概念"})
    assert got == {"特斯拉概念": {"rank": 1, "streak": 2}}
    # 复盘日尚未计算主线时不拿旧日名次冒充
    assert mh._mainline_ranks(tmp_path, date(2026, 11, 12), {"特斯拉概念"}) == {}


# ---------------------------------------------------------------- 入口
def test_build_returns_none_without_lots_or_on_engine_failure(monkeypatch, tmp_path):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr("app.services.holding_analysis.analyze_holdings",
                        lambda *a, **k: {"rows": [], "warnings": []})
    assert mh.build_holdings_linkage(repo, SNAPSHOT) is None

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr("app.services.holding_analysis.analyze_holdings", boom)
    assert mh.build_holdings_linkage(repo, SNAPSHOT) is None
    assert mh.build_holdings_linkage(repo, None) is None


def test_build_researches_at_recap_close_without_live_quotes(monkeypatch, tmp_path):
    seen = {}

    def fake(repo, quote_service, sector_service, position_service, source, **kw):
        seen.update(quote=quote_service, sector=sector_service, source=source, now=kw["now"])
        return {"as_of_date": "2026-11-11", "warnings": [],
                "rows": [_engine_row(), _engine_row(symbol="510300.SH", asset_type="etf")]}

    monkeypatch.setattr("app.services.holding_analysis.analyze_holdings", fake)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    out = mh.build_holdings_linkage(repo, SNAPSHOT, sector_service="S", quote_service="Q")
    assert seen["quote"] is None and seen["sector"] == "S" and seen["source"] == "lots"
    assert seen["now"].isoformat().startswith("2026-11-11T15:31")
    assert [r["symbol"] for r in out["rows"]] == ["600522.SH"]  # 非 A 股股票不进入
    assert out["market_only"] == 1


# ---------------------------------------------------------------- 提示词与推送
def test_prompt_block_has_no_quantity_or_cost_and_lists_conditions():
    holdings = {"rows": [mh._row(_engine_row(_stock_change=4.01), mh._sector_moves(SNAPSHOT), {})]}
    text = mh.holdings_prompt_block(holdings)
    assert "中天科技(600522.SH)" in text and "只看市场联动" in text
    assert "个股 +4.01%" in text and "跑输板块" in text
    assert "相对登记成本 -57.5%(深套)" in text
    assert "收盘 ≥ 26.80(压力) → 站上压力, 联动走强" in text
    assert "400" not in text and "58" not in text  # 不含数量与成本金额
    assert mh.holdings_prompt_block(None) == "" and mh.holdings_prompt_block({"rows": []}) == ""


def test_user_prompt_includes_holdings_block():
    prompt = market_recap._build_user_prompt({"as_of": "2026-11-11"}, [], "", holdings_context="## 持仓联动(程序计算)\n- x")
    assert "## 持仓联动(程序计算)" in prompt


def test_strip_holdings_section_keeps_other_sections():
    content = (
        "## 🗺️ 明日剧本\n表\n\n## 📦 持仓联动\n中天科技跑输板块。\n\n"
        "## ⛔ 失效条件\n- 断板\n\n> ⚠️ 免责\n"
    )
    out = market_recap.strip_holdings_section(content)
    assert "持仓联动" not in out and "中天科技" not in out
    assert "## 🗺️ 明日剧本" in out and "## ⛔ 失效条件" in out and "> ⚠️ 免责" in out
