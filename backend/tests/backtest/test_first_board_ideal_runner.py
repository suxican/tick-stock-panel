"""Offline research keeps raw-price, calendar and read-only report boundaries."""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.huichun_calendar import MarketCalendar, read_market_calendar
from scripts import research_first_board_ideal as runner

FIXTURES = Path(__file__).parents[1] / "fixtures/strategies"


@pytest.fixture
def local_strategies(tmp_path):
    strategies = tmp_path / "strategies/custom"
    strategies.mkdir(parents=True)
    overrides = tmp_path / "user_data/strategy_overrides"
    overrides.mkdir(parents=True)
    for pattern in runner.PATTERNS:
        sid = f"custom_first_board_{pattern}_daily"
        (strategies / f"{sid}.py").write_bytes((FIXTURES / f"{sid}.py").read_bytes())
    (overrides / "custom_first_board_platform_daily.json").write_text(
        json.dumps({"params": {"platform_window": 30, "platform_near_high": .07}}), encoding="utf-8",
    )
    (overrides / "custom_first_board_trend_daily.json").write_text(
        json.dumps({"params": {"trend_window": 50, "trend_min_return": .05}}), encoding="utf-8",
    )
    mode = tmp_path / "user_data/first_board/config.json"
    mode.parent.mkdir()
    mode.write_text('{"revision": 13}', encoding="utf-8")
    return tmp_path


@pytest.fixture
def offline(monkeypatch, local_strategies):
    state = SimpleNamespace(
        latest=date(2025, 1, 10), now=datetime(2025, 1, 10, 16, tzinfo=CN_TZ),
        closed=False, signature=1, read_calls=[], prepare_calls=[], after_prepare=None,
    )
    state.raw = pl.DataFrame([
        {"symbol": "600001.SH", "date": date(2025, 1, day), "open": opening,
         "high": high, "low": low, "close": close, "volume": 1000.0, "amount": 1000000.0}
        for day, opening, high, low, close in (
            (3, 10.0, 10.1, 9.9, 10.0), (6, 10.0, 10.1, 9.9, 10.0),
            (7, 10.0, 10.1, 9.9, 10.0), (8, 10.8, 11.0, 10.7, 11.0),
            (9, 10.9, 11.0, 10.6, 10.8), (10, 11.0, 11.4, 10.9, 11.3),
        )
    ])

    class Store:
        def __init__(self, path):
            assert path == local_strategies
            self.db = self
            self.data_dir = path

        def close(self):
            state.closed = True

    class Repository:
        def __init__(self, store):
            self.store = store

        def execute_all(self, sql):
            assert sql == "SELECT MAX(date) FROM kline_daily"
            return [(state.latest,)]

        def get_instruments(self):
            return pl.DataFrame({"symbol": ["600001.SH"], "name": ["样本"],
                                 "listing_date": [date(2000, 1, 1)]})

    class Reader:
        def __init__(self, repo, *, calendar_path):
            assert calendar_path == runner.BUNDLED_CALENDAR_PATH

        def input_signature(self, cutoff):
            return [("kline_daily", state.signature, cutoff.isoformat())]

        def raw(self, symbols, end, *, start):
            state.read_calls.append((symbols, start, end))
            return state.raw.filter(pl.col("date").is_between(start, end))

        def factors(self, symbols, end):
            assert symbols == ["600001.SH"]
            return pl.DataFrame(schema={"symbol": pl.String, "trade_date": pl.Date,
                                        "ex_factor": pl.Float64})

    def prepare(raw, factors, instruments, days, *, pattern, params, start, end):
        state.prepare_calls.append({"raw_dates": raw["date"].to_list(), "days": days,
                                    "pattern": pattern, "params": params,
                                    "start": start, "end": end})
        if state.after_prepare:
            state.after_prepare()
        # Input generation itself is tested separately. This runner test retains
        # the real ideal evaluator, so shifting or clipping the exit tail changes
        # observable trades rather than merely changing mock call assertions.
        return pl.DataFrame([
            {"symbol": "600001.SH", "date": day, "pattern": pattern,
             "name": "样本", "limit_up_price": 11.0}
            for day in days if start <= day <= end
        ]), {"current_metadata_only": True}

    monkeypatch.setattr(runner, "DataStore", Store)
    monkeypatch.setattr(runner, "KlineRepository", Repository)
    monkeypatch.setattr(runner, "HuichunDataReader", Reader)
    monkeypatch.setattr(runner, "prepare_first_board_ideal_candidates", prepare)
    monkeypatch.setattr(runner, "cn_now", lambda: state.now)
    monkeypatch.setattr(runner, "read_market_calendar", lambda _: MarketCalendar(
        date(2025, 1, 1), date(2025, 12, 31), frozenset({date(2025, 1, 1).toordinal()}),
    ))
    state.data_dir = local_strategies
    return state


def test_saved_params_and_temporary_params_share_precedence_without_writes(local_strategies):
    before = runner._config_signature(local_strategies)
    values = runner._parameters(local_strategies, {
        "platform": {"platform_window": 25}, "trend": {"trend_window": 45},
    })
    assert set(values) == {"platform", "oversold", "trend"}
    assert values["platform"]["params"]["platform_window"] == 25
    assert values["platform"]["params"]["platform_near_high"] == .07
    assert values["platform"]["params"]["lookback_days"] == 35
    assert values["oversold"]["params"]["oversold_min_drawdown"] == .25
    assert values["trend"]["params"]["trend_window"] == 45
    assert values["trend"]["params"]["trend_min_return"] == .05
    assert values["trend"]["params"]["trend_max_return"] == .40
    assert values["platform"]["signal_source"] == "shared_first_board_shape_rules_only"
    assert runner._config_signature(local_strategies) == before


def test_unreproduced_overlay_is_not_silently_ignored(local_strategies):
    path = local_strategies / "user_data/strategy_overrides/custom_first_board_platform_daily.json"
    path.write_text(json.dumps({"overlay_filter": [{"field": "amount", "op": ">", "value": 1}]}),
                    encoding="utf-8")
    with pytest.raises(ValueError, match="叠加条件"):
        runner._parameters(local_strategies, {})


@pytest.mark.parametrize("params", [{"unknown": {}}, {"platform": []}, {"trend": []}])
def test_invalid_parameter_groups_are_rejected(local_strategies, params):
    with pytest.raises(ValueError):
        runner._parameters(local_strategies, params)


def test_raw_read_reserves_exact_exit_tail_and_produces_real_returns(offline):
    before = runner._config_signature(offline.data_dir)
    result = runner.run_research(offline.data_dir, sessions=2)
    assert result["start"] == "2025-01-08"
    assert result["end"] == "2025-01-09"
    assert result["exit_tail"] == "2025-01-10"
    assert result["signal_sessions"] == 2
    assert result["price_source"] == "local_raw_daily"
    assert result["not_executable_backtest"] is True
    assert set(result["reports"]) == {"platform", "oversold", "trend"}
    assert offline.read_calls == [(None, date(2025, 1, 1), date(2025, 1, 10))]
    assert offline.closed
    assert runner._config_signature(offline.data_dir) == before
    for report in result["reports"].values():
        assert report["summary"]["trades"] == 2
        assert [(trade["entry_date"], trade["exit_date"]) for trade in report["trades"]] == [
            ("2025-01-08", "2025-01-09"), ("2025-01-09", "2025-01-10"),
        ]
        assert report["trades"][1]["net_return"] == pytest.approx(11.4 * .9992 / (11 * 1.0003) - 1)
        assert report["input_audit"]["current_metadata_only"] is True


def test_explicit_short_interval_is_not_rejected_by_unused_sixty_session_default(offline):
    result = runner.run_research(offline.data_dir, start=date(2025, 1, 8), end=date(2025, 1, 9))
    assert result["signal_sessions"] == 2
    assert result["exit_tail"] == "2025-01-10"


def test_explicit_year_covers_every_exchange_session_for_all_three_patterns(offline, monkeypatch):
    calendar = read_market_calendar(runner.BUNDLED_CALENDAR_PATH)
    start, end, exit_tail = date(2025, 11, 7), date(2026, 11, 5), date(2026, 11, 6)
    signal_days = calendar.sessions(start=start, end=end)
    price_days = calendar.sessions(start=start, end=exit_tail)
    offline.latest = exit_tail
    offline.now = datetime(2026, 11, 7, 16, tzinfo=CN_TZ)
    offline.raw = pl.DataFrame([
        {"symbol": "600001.SH", "date": day, "open": 10.8, "high": 11.0,
         "low": 10.7, "close": 11.0, "volume": 1000.0, "amount": 1000000.0}
        for day in price_days
    ])
    monkeypatch.setattr(runner, "read_market_calendar", lambda _: calendar)
    result = runner.run_research(offline.data_dir, start=start, end=end)
    assert result["start"] == start.isoformat()
    assert result["end"] == end.isoformat()
    assert result["exit_tail"] == exit_tail.isoformat()
    assert result["signal_sessions"] == len(signal_days)
    assert len(offline.prepare_calls) == 3
    for call in offline.prepare_calls:
        assert call["start"] == start and call["end"] == end
        assert [day for day in call["days"] if start <= day <= end] == signal_days
    assert set(result["reports"]) == {"platform", "oversold", "trend"}
    for report in result["reports"].values():
        assert report["summary"]["signals"] == len(signal_days)
        assert report["summary"]["trades"] == len(signal_days)
        assert [trade["entry_date"] for trade in report["trades"]] == [day.isoformat() for day in signal_days]
        assert [trade["exit_date"] for trade in report["trades"]] == [day.isoformat() for day in price_days[1:]]
        assert report["trades"][0]["exit_date"] == "2025-11-10"
        assert report["trades"][-1]["exit_date"] == "2026-11-06"
    assert offline.read_calls[0][2] == exit_tail
    assert offline.closed


def test_intraday_cutoff_excludes_todays_partial_exit_prices(offline):
    offline.now = datetime(2025, 1, 10, 14, 59, tzinfo=CN_TZ)
    result = runner.run_research(offline.data_dir, sessions=1)
    assert result["end"] == "2025-01-08"
    assert result["exit_tail"] == "2025-01-09"
    assert offline.read_calls[0][2] == date(2025, 1, 9)
    assert result["reports"]["platform"]["trades"][0]["exit_price"] == 11.0


@pytest.mark.parametrize("kwargs", [
    {"start": date(2025, 1, 4), "end": date(2025, 1, 9)},
    {"start": date(2025, 1, 8), "end": date(2025, 1, 10)},
    {"start": date(2025, 1, 9), "end": date(2025, 1, 8)},
])
def test_non_session_or_uncovered_tail_is_rejected_before_price_read(offline, kwargs):
    with pytest.raises(ValueError):
        runner.run_research(offline.data_dir, sessions=1, **kwargs)
    assert not offline.read_calls
    assert offline.closed


@pytest.mark.parametrize("changed", ["source", "config"])
def test_concurrent_input_change_prevents_report_and_closes_store(offline, changed):
    def mutate():
        if changed == "source":
            offline.signature += 1
        else:
            (offline.data_dir / "user_data/first_board/config.json").write_text(
                '{"revision": 14}', encoding="utf-8",
            )
    offline.after_prepare = mutate
    with pytest.raises(ValueError, match="发生变化"):
        runner.run_research(offline.data_dir, sessions=2)
    assert offline.closed


def test_no_raw_history_is_not_a_successful_empty_research(offline):
    offline.latest = None
    with pytest.raises(ValueError, match="没有原始日线"):
        runner.run_research(offline.data_dir, sessions=1)
    assert offline.closed


def test_cli_serializes_valid_report_and_preserves_utf8(monkeypatch, tmp_path):
    output = tmp_path / "reports/ideal.json"
    monkeypatch.setattr(sys, "argv", ["research", "--output", str(output)])
    monkeypatch.setattr(runner, "run_research", lambda *args, **kwargs: {"name": "首板研究", "value": .05})
    runner.main()
    assert json.loads(output.read_text(encoding="utf-8")) == {"name": "首板研究", "value": .05}


def test_cli_existing_destination_never_runs_or_overwrites(monkeypatch, tmp_path):
    output = tmp_path / "keep.json"
    output.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["research", "--output", str(output)])
    monkeypatch.setattr(runner, "run_research", lambda *args, **kwargs: pytest.fail("must not run"))
    with pytest.raises(SystemExit) as raised:
        runner.main()
    assert raised.value.code == 2
    assert output.read_text(encoding="utf-8") == "existing"


def test_cli_serialization_failure_leaves_no_report_or_output_directory(monkeypatch, tmp_path):
    output = tmp_path / "reports/invalid.json"
    monkeypatch.setattr(sys, "argv", ["research", "--output", str(output)])
    monkeypatch.setattr(runner, "run_research", lambda *args, **kwargs: {"mean": float("nan")})
    with pytest.raises(ValueError):
        runner.main()
    assert not output.exists()
    assert not output.parent.exists()


def test_cli_destination_created_during_calculation_is_not_overwritten(monkeypatch, tmp_path):
    output = tmp_path / "raced.json"
    monkeypatch.setattr(sys, "argv", ["research", "--output", str(output)])

    def concurrently_created(*args, **kwargs):
        output.write_text("other process", encoding="utf-8")
        return {"summary": {}}

    monkeypatch.setattr(runner, "run_research", concurrently_created)
    with pytest.raises(FileExistsError):
        runner.main()
    assert output.read_text(encoding="utf-8") == "other process"
