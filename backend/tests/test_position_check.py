"""复盘仓位核对: 收盘持仓占比 vs 前一交易日存档上限。"""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import position_check as pc
from app.services import preferences, regime_builder

D, P = "2026-11-17", "2026-11-16"


def _lot(symbol="600000.SH", qty=1000, buy_date="2026-11-16", created_at="2026-11-16T10:00:00+08:00"):
    return {"id": f"l_{symbol[:6]}", "symbol": symbol, "qty": qty, "cost_price": 10, "buy_date": buy_date,
            "created_at": created_at}


def _entry(symbol="000001.SZ", qty=500, buy_date="2026-11-10", sell_date="2026-11-18"):
    return {"symbol": symbol, "qty": qty, "buy_date": buy_date, "sell_date": sell_date}


def _archive(tmp_path, day=P, versions=None):
    root = tmp_path / "user_data" / "environment_conclusion"
    root.mkdir(parents=True, exist_ok=True)
    versions = versions if versions is not None else [
        {"recorded_at": f"{day}T16:40:00+08:00", "trigger": "pipeline",
         "conclusion": {"date": day, "cell": "strong_expand", "effective_cap_pct": 80}}]
    (root / f"{day}.json").write_text(json.dumps({"date": day, "versions": versions}), encoding="utf-8")


def _regime(tmp_path, days=("2026-11-13", P, D)):
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [date.fromisoformat(d) for d in days], "state": ["strong"] * len(days), "score": [60] * len(days)}))


# ── 持仓 ──

def test_holdings_on_date_boundaries():
    lots = [_lot("600000.SH", 1000, "2026-11-17"), _lot("600001.SH", 200, "2026-11-18")]
    entries = [_entry("000001.SZ", 500, "2026-11-10", "2026-11-18"),   # 次日才卖: 当日在仓
               _entry("000002.SZ", 300, "2026-11-10", "2026-11-17")]   # 当日卖出: 不计
    holdings, inferred, unknown = pc.holdings_on(lots, entries, D)
    assert holdings == {"600000.SH": 1000.0, "000001.SZ": 500.0}
    assert (inferred, unknown) == (0, 0)


def test_holdings_infers_open_lot_buy_date_from_created_at():
    lots = [_lot("600000.SH", 1000, None, "2026-11-12T02:00:00+00:00"),   # 北京时间 11-12 10:00
            _lot("600001.SH", 200, "", "2026-11-18T02:00:00+00:00")]
    holdings, inferred, unknown = pc.holdings_on(lots, [], D)
    assert holdings == {"600000.SH": 1000.0}
    assert inferred == 2 and unknown == 0


def test_holdings_skips_closed_entry_without_buy_date():
    holdings, inferred, unknown = pc.holdings_on([], [_entry(buy_date=None)], D)
    assert holdings == {} and (inferred, unknown) == (0, 1)


def test_holdings_sums_same_symbol_lots():
    holdings, _, _ = pc.holdings_on([_lot("600000.SH", 1000), _lot("600000.SH", 500)], [], D)
    assert holdings == {"600000.SH": 1500.0}


# ── 上限 ──

def test_cap_uses_previous_trading_day_archive(tmp_path):
    _regime(tmp_path)
    _archive(tmp_path)
    assert pc.applicable_cap(tmp_path, D) == {"cap_pct": 80, "cap_date": P, "cell": "strong_expand", "reason": None}


def test_cap_ignores_versions_after_open(tmp_path):
    _regime(tmp_path)
    _archive(tmp_path, versions=[
        {"recorded_at": f"{P}T16:40:00+08:00", "trigger": "pipeline",
         "conclusion": {"date": P, "cell": "strong_expand", "effective_cap_pct": 80}},
        {"recorded_at": f"{D}T09:10:00+08:00", "trigger": "preset",
         "conclusion": {"date": P, "cell": "strong_expand", "effective_cap_pct": 60}},
        {"recorded_at": f"{D}T10:05:00+08:00", "trigger": "preset",
         "conclusion": {"date": P, "cell": "strong_expand", "effective_cap_pct": 100}},
    ])
    assert pc.applicable_cap(tmp_path, D)["cap_pct"] == 60


def test_cap_missing_archive_or_unset_preset(tmp_path):
    _regime(tmp_path)
    assert pc.applicable_cap(tmp_path, D) == {"cap_pct": None, "cap_date": P, "cell": None, "reason": "前一交易日无结论存档"}
    _archive(tmp_path, versions=[{"recorded_at": f"{P}T16:40:00+08:00", "trigger": "pipeline",
                                  "conclusion": {"date": P, "cell": "weak_flat", "effective_cap_pct": None}}])
    out = pc.applicable_cap(tmp_path, D)
    assert out["cap_pct"] is None and out["reason"] == "前一交易日该格未设置预设" and out["cell"] == "weak_flat"


def test_cap_without_previous_trading_day(tmp_path):
    _regime(tmp_path, days=(D,))
    assert pc.applicable_cap(tmp_path, D)["reason"] == "缺少前一交易日"


# ── 单日核对 ──

CAP = {"cap_pct": 80, "cap_date": P, "cell": "strong_expand", "reason": None}


@pytest.mark.parametrize("value,exceeded", [(400000.0, False), (450000.0, True), (400000.01, True)])
def test_check_day_compares_ratio_with_cap(value, exceeded):
    out = pc.check_day(D, {"600000.SH": 1000}, {"600000.SH": value / 1000}, 500000.0, CAP)
    assert out["exceeded"] is exceeded and out["reason"] is None
    assert out["ratio_pct"] == pytest.approx(value / 500000 * 100)
    assert out["position_value"] == pytest.approx(value)


def test_check_day_equal_to_cap_is_not_exceeded():
    out = pc.check_day(D, {"600000.SH": 1000}, {"600000.SH": 400.0}, 500000.0, CAP)
    assert out["ratio_pct"] == 80 and out["exceeded"] is False


def test_check_day_empty_position_is_zero():
    out = pc.check_day(D, {}, {}, 500000.0, CAP)
    assert out["ratio_pct"] == 0 and out["exceeded"] is False


@pytest.mark.parametrize("kwargs,reason", [
    ({"account_size": None}, "未填写账户规模"),
    ({"closes": {}}, "缺收盘价"),
    ({"intraday": True}, "盘中不判定"),
    ({"cap": {**CAP, "cap_pct": None, "reason": "前一交易日无结论存档"}}, "前一交易日无结论存档"),
])
def test_check_day_undecidable(kwargs, reason):
    args = {"closes": {"600000.SH": 300.0}, "account_size": 500000.0, "cap": CAP, "intraday": False, **kwargs}
    out = pc.check_day(D, {"600000.SH": 1000}, args["closes"], args["account_size"], args["cap"], intraday=args["intraday"])
    assert out["exceeded"] is None and out["reason"] == reason


def test_check_day_keeps_ratio_when_only_cap_missing():
    out = pc.check_day(D, {"600000.SH": 1000}, {"600000.SH": 300.0}, 500000.0,
                       {**CAP, "cap_pct": None, "reason": "前一交易日无结论存档"})
    assert out["ratio_pct"] == 60 and out["exceeded"] is None


# ── 汇总 ──

class FakeRepo:
    def __init__(self, data_dir, closes):
        self.store = SimpleNamespace(data_dir=data_dir)
        self.closes = closes
        self.calls = 0

    def get_enriched_range(self, start, end, symbols=None, columns=None):
        self.calls += 1
        rows = [{"symbol": s, "date": date.fromisoformat(d), "close": c, "raw_close": c}
                for (d, s), c in self.closes.items() if start.isoformat() <= d <= end.isoformat()]
        return pl.DataFrame(rows, schema={"symbol": pl.Utf8, "date": pl.Date, "close": pl.Float64, "raw_close": pl.Float64})


def test_build_position_check_today_and_stats(tmp_path, monkeypatch):
    days = ("2026-11-12", "2026-11-13", P, D)
    _regime(tmp_path, days=days)
    _archive(tmp_path, day="2026-11-12", versions=[{"recorded_at": "2026-11-12T16:40:00+08:00", "trigger": "pipeline",
             "conclusion": {"date": "2026-11-12", "cell": "weak_flat", "effective_cap_pct": 10}}])
    _archive(tmp_path, day=P)   # 2026-11-13 无存档: 不进统计
    monkeypatch.setattr(preferences, "get_account_size_history",
                        lambda: [{"effective_date": "2026-11-01", "amount": 500000.0}])
    lots = [_lot("600000.SH", 1000, "2026-11-12")]
    repo = FakeRepo(tmp_path, {("2026-11-13", "600000.SH"): 100.0, (D, "600000.SH"): 300.0})
    today, stats = pc.build_position_check(repo, D, lots, [], intraday=False)
    assert today["ratio_pct"] == 60 and today["cap_pct"] == 80 and today["exceeded"] is False
    # 11-13: 20% vs 上限 10% (11-12 存档) → 超限; 11-17: 60% vs 80% → 未超限
    assert stats["checked"] == 2 and stats["exceeded"] == 1
    assert {row["cell"]: (row["checked"], row["exceeded"]) for row in stats["by_cell"]} == {
        "weak_flat": (1, 1), "strong_expand": (1, 0)}
    assert next(r for r in stats["by_cell"] if r["cell"] == "weak_flat")["label"] == "弱·平量"


def test_stats_reads_closes_once(tmp_path, monkeypatch):
    days = [f"2026-11-{d:02d}" for d in (10, 11, 12, 13, 16, 17)]
    _regime(tmp_path, days=days)
    for day in days[:-1]:
        _archive(tmp_path, day=day, versions=[{"recorded_at": f"{day}T16:40:00+08:00", "trigger": "pipeline",
                 "conclusion": {"date": day, "cell": "strong_flat", "effective_cap_pct": 50}}])
    monkeypatch.setattr(preferences, "get_account_size_history",
                        lambda: [{"effective_date": "2026-11-01", "amount": 500000.0}])
    repo = FakeRepo(tmp_path, {(d, "600000.SH"): 100.0 for d in days})
    pc.build_position_check(repo, D, [_lot("600000.SH", 1000, "2026-11-01")], [], intraday=False)
    assert repo.calls == 1


def test_account_size_after_day_is_not_used(tmp_path, monkeypatch):
    _regime(tmp_path)
    _archive(tmp_path)
    monkeypatch.setattr(preferences, "get_account_size_history",
                        lambda: [{"effective_date": "2026-11-20", "amount": 500000.0}])
    repo = FakeRepo(tmp_path, {(D, "600000.SH"): 100.0})
    today, stats = pc.build_position_check(repo, D, [_lot()], [], intraday=False)
    assert today["reason"] == "未填写账户规模" and today["exceeded"] is None
    assert stats["checked"] == 0
