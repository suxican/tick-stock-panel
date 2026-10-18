from datetime import date, timedelta

import polars as pl
import pytest

from app.backtest import first_board as research
from app.strategy.first_board import FirstBoardRules


class Repository:
    def __init__(self, panel):
        self.panel = panel

    def get_instruments(self):
        return self.panel.select("symbol", "name").unique()

    def get_daily_batch(self, symbols, start, end, columns=None):
        frame = self.panel.filter(pl.col("symbol").is_in(symbols) & pl.col("date").is_between(start, end))
        return frame.select([column for column in columns if column in frame.columns]) if columns else frame

    def get_index_daily(self, symbol, start, end, columns=None):
        return self.panel.filter(pl.col("date").is_between(start, end)).select("date").unique().sort("date")


@pytest.fixture
def panel(monkeypatch):
    monkeypatch.setattr(research, "cn_today", lambda: date(2026, 3, 1))
    days = []
    day = date(2025, 9, 1)
    while len(days) < 100:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return pl.DataFrame([{
        "symbol": symbol, "name": "测试股票", "date": value,
        "open": 5.0, "high": 5.05, "low": 4.95, "close": 5.0,
        "raw_close": 10.0, "raw_high": 10.1, "raw_low": 9.9,
        "amount": 80_000_000.0, "volume": 8000.0, "turnover_rate": 5.0,
    } for value in days for symbol in ("600001.SH", "000001.SZ")])


def rules():
    return FirstBoardRules(min_history_days=20, enabled_patterns=["platform"])


def window(panel):
    days = panel["date"].unique().sort().to_list()
    return days[60], days[-1]


def test_historical_narrow_data_reconstructs_limit_and_uses_adjusted_returns(panel):
    start, end = window(panel)
    result = research.run_first_board_research(Repository(panel), start=start, end=end, rules=rules())
    assert result["summary"]["candidates"] == 80
    assert result["summary"]["seal_observed"] == 80
    assert result["samples"][0]["limit_up"] == 11.0
    assert result["samples"][0]["state"] == "watch"
    assert result["summary"]["next_close_mean"] == pytest.approx(0.0)
    assert result["coverage"]["missing_outcomes"] == 2
    assert result["capabilities"]["minute_execution"]["available"] is False


def test_explicit_no_limit_is_not_reconstructed_in_research(panel):
    start, end = window(panel)
    target = (pl.col("date") == start) & (pl.col("symbol") == "600001.SH")
    panel = panel.with_columns(pl.when(target).then(None).otherwise(10.995).alias("limit_up_price"))
    result = research.run_first_board_research(Repository(panel), start=start, end=end, rules=rules())
    sample = next(row for row in result["samples"] if row["date"] == str(start) and row["symbol"] == "600001.SH")
    assert sample["limit_up"] is None
    assert sample["sealed"] is None and sample["touched"] is None
    assert "missing_limit_reference" in sample["gap_reasons"]


def test_historical_chinext_does_not_use_post_reform_cached_limit():
    candidate = {"symbol": "300001.SZ", "reference_adjusted_close": 5.0}
    current = {"raw_close": 11.0, "close": 5.5, "limit_up_price": 11.995}
    assert research._limit(candidate, current, date(2020, 8, 21)) == 11.0
    assert research._limit(candidate, current, date(2020, 8, 24)) == 12.0


def test_research_sample_limit_does_not_change_denominators(panel):
    start, end = window(panel)
    result = research.run_first_board_research(Repository(panel), start=start, end=end, rules=rules(), sample_limit=1)
    assert len(result["samples"]) == 1
    assert result["samples_truncated"] is True
    assert result["summary"]["candidates"] == result["total_samples"] == 80


def test_next_day_split_does_not_become_a_fifty_percent_loss(panel):
    start, end = window(panel)
    next_day = panel.filter(pl.col("date") > start)["date"].min()
    split = (pl.col("date") == next_day) & (pl.col("symbol") == "600001.SH")
    panel = panel.with_columns(
        pl.when(split).then(5.0).otherwise(pl.col("raw_close")).alias("raw_close"),
        pl.when(split).then(5.05).otherwise(pl.col("raw_high")).alias("raw_high"),
        pl.when(split).then(4.95).otherwise(pl.col("raw_low")).alias("raw_low"),
    )
    result = research.run_first_board_research(Repository(panel), start=start, end=end, rules=rules())
    sample = next(row for row in result["samples"] if row["date"] == str(start) and row["symbol"] == "600001.SH")
    assert sample["next_open_return"] == pytest.approx(0.0)
    assert sample["next_close_return"] == pytest.approx(0.0)


def test_missing_next_session_never_skips_to_later_price(panel):
    start, end = window(panel)
    next_day = panel.filter(pl.col("date") > start)["date"].min()
    panel = panel.filter(~((pl.col("date") == next_day) & (pl.col("symbol") == "600001.SH")))
    result = research.run_first_board_research(Repository(panel), start=start, end=end, rules=rules())
    sample = next(row for row in result["samples"] if row["date"] == str(start) and row["symbol"] == "600001.SH")
    assert sample["next_close_return"] is None
    assert "missing_next_session" in sample["gap_reasons"]


def test_chronological_comparison_purges_cross_split_returns(panel):
    start, end = window(panel)
    result = research.compare_first_board_rules(Repository(panel), start=start, end=end, rules=rules())
    assert result["train"]["session_count"] == 28
    assert result["validation"]["session_count"] == 12
    assert result["train"]["sessions"][-1] < result["validation"]["sessions"][0]
    baseline = result["variants"][0]
    assert baseline["train"]["next_close_count"] == 54
    assert baseline["validation"]["next_close_count"] == 22
    assert result["auto_applied"] is False
    assert result["recommendation"] is None


def test_validation_prices_cannot_change_training_selection(panel):
    start, end = window(panel)
    initial = research.compare_first_board_rules(Repository(panel), start=start, end=end, rules=rules())
    split = date.fromisoformat(initial["validation"]["sessions"][0])
    changed = panel.with_columns(
        pl.when(pl.col("date") >= split).then(11.0).otherwise(pl.col("raw_close")).alias("raw_close"),
        pl.when(pl.col("date") >= split).then(11.0).otherwise(pl.col("raw_high")).alias("raw_high"),
        pl.when(pl.col("date") >= split).then(5.5).otherwise(pl.col("close")).alias("close"),
        pl.when(pl.col("date") >= split).then(5.5).otherwise(pl.col("high")).alias("high"),
    )
    actual = research.compare_first_board_rules(Repository(changed), start=start, end=end, rules=rules())
    assert actual["training_selected_id"] == initial["training_selected_id"] == "baseline"
    assert [v["train"] for v in actual["variants"]] == [v["train"] for v in initial["variants"]]
    assert actual["data_window"]["fingerprint"] != initial["data_window"]["fingerprint"]


def test_missing_columns_duplicates_future_and_unsupported_modes_rejected(panel):
    start, end = window(panel)
    with pytest.raises(ValueError, match="必要日线字段"):
        research.run_first_board_research(Repository(panel.drop("raw_close")), start=start, end=end, rules=rules())
    with pytest.raises(ValueError, match="重复"):
        research.run_first_board_research(Repository(pl.concat([panel, panel.head(1)])), start=start, end=end, rules=rules())
    with pytest.raises(ValueError, match="已完成"):
        research.run_first_board_research(Repository(panel), start=start, end=date(2026, 3, 1), rules=rules())
    with pytest.raises(ValueError, match="分钟成交"):
        research.run_first_board_research(Repository(panel), start=start, end=end, rules=rules(), mode="minute")
