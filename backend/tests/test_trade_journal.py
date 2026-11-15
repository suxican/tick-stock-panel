"""交易记录域模块: 校验 / 构建 / 存取 / 盈亏口径 / 统计。"""
from __future__ import annotations

from datetime import date

import pytest

from app.strategy import trade_journal as tj

LOT = {"id": "lot_a1", "symbol": "600519.SH", "qty": 100, "cost_price": 1680.0,
       "buy_date": "2026-11-05", "target_pct": 10, "stop_pct": 5}
TODAY = date(2026, 11, 13)


def _entry(**kw):
    base = dict(sell_price=1722.0, sell_date="2026-11-13", reason="planned_profit", note="",
                name="贵州茅台", had_invalidation=True, buy_phase="rally", hold_days=6)
    base.update(kw)
    return tj.build_entry(LOT, **base)


def test_build_entry_freezes_lot_fields_and_derives_id():
    e = _entry()
    assert e["id"] == "tj_lot_a1" and e["lot_id"] == "lot_a1"
    assert (e["qty"], e["cost_price"], e["buy_date"], e["stop_pct"]) == (100, 1680.0, "2026-11-05", 5)
    assert (e["name"], e["had_invalidation"], e["buy_phase"], e["hold_days"]) == ("贵州茅台", True, "rally", 6)
    assert e["created_at"]
    assert tj.pnl_pct(e) == pytest.approx(2.5)  # 百分数, 不是 0.025


@pytest.mark.parametrize("kw, msg", [
    (dict(sell_price=0), "卖出价"), (dict(sell_price=True), "卖出价"),
    (dict(sell_date="2026/11/13"), "卖出日"), (dict(sell_date="2026-11-14"), "不能晚于今天"),
    (dict(sell_date="2026-11-04"), "不能早于买入日"), (dict(reason="other"), "归因"),
    (dict(note="x" * 201), "备注"),
])
def test_validate_close_rejects(kw, msg):
    args = dict(sell_price=1722.0, sell_date="2026-11-13", reason="planned_profit", note="")
    args.update(kw)
    with pytest.raises(ValueError, match=msg):
        tj.validate_close(LOT, today=TODAY, **args)


def test_validate_close_allows_missing_buy_date():
    tj.validate_close({**LOT, "buy_date": None}, 1722.0, "2026-11-13", "unplanned", "", today=TODAY)


def test_store_roundtrip_overwrites_same_lot_and_skips_corrupt(tmp_path):
    tj.save_one(tmp_path, _entry())
    tj.save_one(tmp_path, _entry(sell_price=1700.0))
    (tmp_path / "user_data" / "trade_journal" / "tj_bad.json").write_text("{", encoding="utf-8")
    rows = tj.load_all(tmp_path)
    assert [r["sell_price"] for r in rows] == [1700.0]
    assert tj.delete_one(tmp_path, "tj_lot_a1") is True
    assert tj.load_all(tmp_path) == []


def test_stats_groups_and_ignores_breakeven_and_missing_hold_days():
    es = [_entry(sell_price=1848.0, hold_days=4),                                          # +10%
          _entry(sell_price=1596.0, reason="late_stop", buy_phase="ebb", hold_days=None),  # -5%
          _entry(sell_price=1680.0, reason="early_exit", buy_phase=None, hold_days=2)]     # 0%
    s = tj.stats(es)
    assert (s["total"], s["wins"], s["losses"]) == (3, 1, 1)
    assert s["win_rate"] == pytest.approx(100 / 3)
    assert (s["avg_win_pct"], s["avg_loss_pct"], s["payoff_ratio"]) == pytest.approx((10, 5, 2))
    assert s["avg_hold_days"] == pytest.approx(3)
    assert [g["key"] for g in s["by_reason"]] == ["planned_profit", "early_exit", "late_stop"]
    assert s["by_reason"][0] == {"key": "planned_profit", "label": "按计划止盈", "total": 1,
                                 "win_rate": pytest.approx(100), "avg_pnl_pct": pytest.approx(10)}
    phases = {g["key"]: g["label"] for g in s["by_phase"]}
    assert phases["unknown"] == "未知" and phases["rally"] == "主升"


def test_stats_empty_and_one_sided():
    assert tj.stats([]) is None
    one = tj.stats([_entry(sell_price=1848.0)])
    assert one["payoff_ratio"] is None and one["avg_loss_pct"] is None
