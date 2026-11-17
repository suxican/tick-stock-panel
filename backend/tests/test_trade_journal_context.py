"""结清时冻结的上下文: 持有交易日数 / 买入日阶段 / 是否已登记失效条件。"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.services import holding_analysis
from app.services import trade_journal_context as ctx
from app.services.holding_analysis_store import HoldingAnalysisStore


def test_hold_days_counts_sessions_and_degrades():
    assert ctx.hold_days("2026-11-05", "2026-11-13") == 6     # 周四 → 次周周五
    assert ctx.hold_days("2026-11-13", "2026-11-13") == 0     # 建仓当日记零
    assert ctx.hold_days(None, "2026-11-13") is None
    assert ctx.hold_days("2026-11-05", "2026-11-14") is None  # 周六非交易日
    assert ctx.hold_days("bad", "2026-11-13") is None
    assert ctx.hold_days("1980-01-02", "2026-11-13") is None  # 日历不覆盖


def test_buy_phase_reads_regime_history(tmp_path, monkeypatch):
    df = pl.DataFrame({"date": [date(2026, 11, 5), date(2026, 11, 6)], "phase": ["rally", None]})
    monkeypatch.setattr("app.services.regime_builder.load_regime_history", lambda _d: df)
    assert ctx.buy_phase(tmp_path, "2026-11-05") == "rally"
    assert ctx.buy_phase(tmp_path, "2026-11-06") is None
    assert ctx.buy_phase(tmp_path, "2026-11-09") is None
    assert ctx.buy_phase(tmp_path, None) is None


def test_buy_phase_without_history(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.regime_builder.load_regime_history", lambda _d: pl.DataFrame())
    assert ctx.buy_phase(tmp_path, "2026-11-05") is None


def test_invalidation_flags(tmp_path):
    key = holding_analysis._key("lots", "", "600519.SH")
    HoldingAnalysisStore(tmp_path).save_profile(key, {"invalidation": "跌破 20 日线"}, expected_version=0)
    blank = holding_analysis._key("lots", "", "000001.SZ")
    HoldingAnalysisStore(tmp_path).save_profile(blank, {"invalidation": "  "}, expected_version=0)
    flags = holding_analysis.lots_invalidation_flags(tmp_path, ["600519.SH", "000001.SZ", "300750.SZ"])
    assert flags == {"600519.SH": True, "000001.SZ": False, "300750.SZ": False}


def test_invalidation_flags_unreadable_store_is_all_false(tmp_path):
    folder = tmp_path / "user_data" / "holding_analysis"
    folder.mkdir(parents=True)
    (folder / "state.json").write_text("{", encoding="utf-8")
    assert holding_analysis.lots_invalidation_flags(tmp_path, ["600519.SH"]) == {"600519.SH": False}



def _cells(monkeypatch, rows):
    df = pl.DataFrame(rows, schema={"date": pl.Date, "state": pl.Utf8, "capital_volume": pl.Utf8})
    monkeypatch.setattr("app.services.regime_builder.load_regime_history", lambda _d: df)


def test_buy_cell_uses_previous_trading_day(tmp_path, monkeypatch):
    _cells(monkeypatch, [{"date": date(2026, 11, 4), "state": "lean_strong", "capital_volume": "expand"},
                         {"date": date(2026, 11, 5), "state": "weak", "capital_volume": "shrink"}])
    assert ctx.buy_cell(tmp_path, "2026-11-05") == "strong_expand"
    assert ctx.buy_cell(tmp_path, "2026-11-06") == "weak_shrink"


def test_buy_cell_uses_last_day_before_non_trading_buy_date(tmp_path, monkeypatch):
    _cells(monkeypatch, [{"date": date(2026, 11, 6), "state": "range", "capital_volume": "flat"}])
    assert ctx.buy_cell(tmp_path, "2026-11-08") == "range_flat"   # 周日


@pytest.mark.parametrize("rows,buy_date", [
    ([{"date": date(2026, 11, 5), "state": "strong", "capital_volume": "expand"}], "2026-11-05"),  # 无前一交易日
    ([{"date": date(2026, 11, 4), "state": "strong", "capital_volume": None}], "2026-11-05"),
    ([{"date": date(2026, 11, 4), "state": None, "capital_volume": "expand"}], "2026-11-05"),
    ([{"date": date(2026, 11, 4), "state": "mystery", "capital_volume": "expand"}], "2026-11-05"),
    ([{"date": date(2026, 11, 4), "state": "strong", "capital_volume": "expand"}], None),
    ([{"date": date(2026, 11, 4), "state": "strong", "capital_volume": "expand"}], "bad"),
])
def test_buy_cell_missing_inputs_are_none(tmp_path, monkeypatch, rows, buy_date):
    _cells(monkeypatch, rows)
    assert ctx.buy_cell(tmp_path, buy_date) is None


def test_buy_cell_without_history_columns(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.regime_builder.load_regime_history",
                        lambda _d: pl.DataFrame({"date": [date(2026, 11, 4)], "state": ["strong"]}))
    assert ctx.buy_cell(tmp_path, "2026-11-05") is None
