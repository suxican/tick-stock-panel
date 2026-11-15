"""复盘快照「今日执行」: 新建 / 结清 / 触及止损价仍持有 / 累计统计 + 提示词文本段。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import holding_analysis, market_recap
from app.services import market_recap_execution as mx
from app.services.holding_analysis_store import HoldingAnalysisStore
from app.strategy import lots as lots_domain
from app.strategy import trade_journal as tj

AS_OF = "2026-11-13"
NAMES = {"600519.SH": "贵州茅台", "000001.SZ": "平安银行", "300750.SZ": "宁德时代", "601318.SH": "中国平安"}


def _repo(tmp_path):
    return SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_name_map=lambda symbols=None: dict(NAMES),
    )


def _lot(tmp_path, lot_id, symbol, *, cost=100.0, buy_date="2026-11-05", stop_pct=5, qty=700):
    lot = lots_domain.normalize_lot({"id": lot_id, "symbol": symbol, "qty": qty, "cost_price": cost,
                                     "buy_date": buy_date, "target_pct": 10, "stop_pct": stop_pct})
    lots_domain.save_one(tmp_path, lot)
    return lot


def _closed(tmp_path, lot_id, symbol, *, cost=1680.0, sell_price=1722.0, sell_date=AS_OF,
            buy_date="2026-11-05", reason="planned_profit", note="", had_invalidation=True):
    lot = {"id": lot_id, "symbol": symbol, "qty": 700, "cost_price": cost, "buy_date": buy_date,
           "target_pct": 10, "stop_pct": 5}
    tj.save_one(tmp_path, tj.build_entry(
        lot, sell_price=sell_price, sell_date=sell_date, reason=reason, note=note,
        name=NAMES[symbol], had_invalidation=had_invalidation, buy_phase="rally", hold_days=6))


def _closes(monkeypatch, closes):
    monkeypatch.setattr(mx, "_raw_closes", lambda repo, day, symbols: closes)


def test_execution_lists_opened_closed_breached_and_stats(tmp_path, monkeypatch):
    _lot(tmp_path, "lot_new", "000001.SZ", buy_date=AS_OF, stop_pct=0)          # 当日买入, 未登记失效条件
    _lot(tmp_path, "lot_brk", "300750.SZ", cost=100.0, stop_pct=5)              # 止损价 95.0
    _lot(tmp_path, "lot_edge", "601318.SH", cost=100.0, stop_pct=5)             # 收盘恰为 95.0
    _lot(tmp_path, "lot_ok", "600519.SH", cost=100.0, stop_pct=5)               # 95.01 不算
    _closed(tmp_path, "lot_done", "600519.SH")                                  # 当日结清 +2.5%
    _closes(monkeypatch, {"300750.SZ": 94.9, "601318.SH": 95.0, "600519.SH": 95.01, "000001.SZ": 1.0})

    out = mx.build_execution(_repo(tmp_path), {"as_of": AS_OF, "intraday": False})

    assert out["as_of"] == AS_OF
    assert out["opened"] == [{"symbol": "000001.SZ", "name": "平安银行", "has_invalidation": False}]
    assert out["closed"] == [{"symbol": "600519.SH", "name": "贵州茅台", "pnl_pct": pytest.approx(2.5),
                              "hold_days": 6, "reason": "planned_profit", "reason_label": "按计划止盈"}]
    assert out["stop_breached"] == [
        {"symbol": "300750.SZ", "name": "宁德时代", "stop_price": 95.0, "close": 94.9},
        {"symbol": "601318.SH", "name": "中国平安", "stop_price": 95.0, "close": 95.0},
    ]
    assert out["stats"]["total"] == 1 and out["stats"]["win_rate"] == pytest.approx(100)


def test_opened_marks_registered_invalidation_and_same_day_round_trip(tmp_path, monkeypatch):
    _lot(tmp_path, "lot_new", "000001.SZ", buy_date=AS_OF, stop_pct=0)
    HoldingAnalysisStore(tmp_path).save_profile(
        holding_analysis._key("lots", "", "000001.SZ"), {"invalidation": "跌破 5 日线"}, expected_version=0)
    _closed(tmp_path, "lot_rt", "600519.SH", buy_date=AS_OF, had_invalidation=True)  # 当日买入当日结清
    _closes(monkeypatch, {})
    out = mx.build_execution(_repo(tmp_path), {"as_of": AS_OF, "intraday": False})
    assert {o["symbol"]: o["has_invalidation"] for o in out["opened"]} == {"000001.SZ": True, "600519.SH": True}


@pytest.mark.parametrize("snapshot, closes", [
    ({"as_of": AS_OF, "intraday": True}, {"300750.SZ": 90.0}),   # 盘中不判定
    ({"as_of": AS_OF, "intraday": False}, None),                 # 取不到收盘价
    ({"as_of": AS_OF, "intraday": False}, {}),                   # 单只无收盘价
])
def test_execution_skips_breach_when_close_not_final(tmp_path, monkeypatch, snapshot, closes):
    _lot(tmp_path, "lot_brk", "300750.SZ", cost=100.0, stop_pct=5)
    _closed(tmp_path, "lot_done", "600519.SH")
    _closes(monkeypatch, closes)
    assert mx.build_execution(_repo(tmp_path), snapshot)["stop_breached"] == []


def test_execution_stats_excludes_entries_sold_after_as_of(tmp_path, monkeypatch):
    _closed(tmp_path, "lot_a", "600519.SH", sell_date="2026-11-12")
    _closed(tmp_path, "lot_b", "000001.SZ", sell_date="2026-11-16", sell_price=1000.0)
    _closes(monkeypatch, {})
    out = mx.build_execution(_repo(tmp_path), {"as_of": AS_OF, "intraday": False})
    assert out["closed"] == [] and out["stats"]["total"] == 1 and out["stats"]["losses"] == 0


def test_execution_none_when_nothing_to_show(tmp_path, monkeypatch):
    assert mx.build_execution(_repo(tmp_path), {"as_of": AS_OF, "intraday": False}) is None
    _lot(tmp_path, "lot_old", "600519.SH", cost=100.0, stop_pct=5)   # 老批次, 未触及止损
    _closes(monkeypatch, {"600519.SH": 99.0})
    assert mx.build_execution(_repo(tmp_path), {"as_of": AS_OF, "intraday": False}) is None
    assert mx.build_execution(_repo(tmp_path), None) is None


def test_prompt_block_has_no_qty_price_cost_or_note(tmp_path, monkeypatch):
    _lot(tmp_path, "lot_brk", "300750.SZ", cost=100.0, stop_pct=5)
    _lot(tmp_path, "lot_new", "000001.SZ", buy_date=AS_OF, stop_pct=0)
    _closed(tmp_path, "lot_done", "600519.SH", note="追高了")
    _closes(monkeypatch, {"300750.SZ": 94.9})
    block = mx.execution_prompt_block(mx.build_execution(_repo(tmp_path), {"as_of": AS_OF, "intraday": False}))
    assert block.startswith("## 今日执行(程序判定)")
    for leaked in ("700", "1680", "1722", "追高了", "94.9", "95.0"):
        assert leaked not in block
    for expected in ("贵州茅台", "+2.50%", "按计划止盈", "持有 6 个交易日", "平安银行", "未登记失效条件",
                     "宁德时代", "累计 1 笔"):
        assert expected in block


def test_prompt_block_empty_for_none():
    assert mx.execution_prompt_block(None) == ""


def test_user_prompt_includes_execution_context_only_when_given():
    base = market_recap._build_user_prompt({"as_of": AS_OF}, [], "")
    assert "今日执行" not in base
    prompt = market_recap._build_user_prompt(
        {"as_of": AS_OF}, [], "", execution_context="## 今日执行(程序判定)\n- x")
    assert "## 今日执行(程序判定)\n- x" in prompt


def test_system_prompt_declares_execution_section():
    prompt = market_recap._SYSTEM_PROMPT
    assert "## 🧾 执行复盘" in prompt and "不得改判归因" in prompt
    assert prompt.index("## 📦 持仓联动") < prompt.index("## 🧾 执行复盘") < prompt.index("## ⛔ 失效条件")


def test_push_strips_execution_section_like_holdings():
    content = ("## 🗺️ 明日剧本\n表\n\n## 📦 持仓联动\n甲跑输。\n\n## 🧾 执行复盘\n乙按计划止盈。\n\n"
               "## ⛔ 失效条件\n- 断板\n")
    out = market_recap.strip_holdings_section(content)
    assert "执行复盘" not in out and "乙" not in out and "甲" not in out
    assert "## 🗺️ 明日剧本" in out and "## ⛔ 失效条件" in out
