"""Daily first-board research uses the normal strategy save, screen and backtest paths."""

from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.api.strategy import StrategyCodeSaveRequest, _strategy_detail, save_strategy_code_core
from app.backtest.engine import BacktestEngine
from app.backtest.strategy import (
    StrategyBacktestConfig,
    StrategyBacktestService,
    StrategyDependencyResolver,
)
from app.market_time import CN_TZ
from app.strategy import config, market_data
from app.strategy.engine import StrategyDataContext, StrategyEngine
from app.strategy.first_board import FirstBoardRules, build_candidates, filter_daily_pattern_history

FIXTURES = Path(__file__).parent / "fixtures/strategies"
PATTERNS = ("platform", "trend", "oversold")


def _days(count):
    result = []
    day = date(2025, 1, 2)
    while len(result) < count:
        if day.weekday() < 5:
            result.append(day)
        day += timedelta(days=1)
    return result


def _history(pattern, count=80):
    rows = []
    for index, day in enumerate(_days(count)):
        if pattern == "platform":
            close = 10.0
        elif pattern == "trend":
            close = 10.0 + .02 * index
        else:
            close = 14.0 - 4.0 * max(0, index - count + 40) / 39
        high, low = (11.0, 9.5) if pattern == "platform" else (close + .1, close - .1)
        rows.append({
            "symbol": "600001.SH", "name": "研究样本", "date": day,
            "open": close, "high": high, "low": low, "close": close,
            "raw_close": close, "raw_high": high, "raw_low": low,
            "limit_up_price": round((rows[-1]["close"] if rows else close) * 1.1, 2) - .005,
            "volume": 100_000.0, "amount": 1_000_000.0,
            "signal_limit_up": False, "signal_limit_down": False,
        })
    return pl.DataFrame(rows)


@pytest.fixture(params=PATTERNS)
def registered(request, tmp_path, monkeypatch):
    pattern = request.param
    sid = f"custom_first_board_{pattern}_daily"
    source = FIXTURES / f"{sid}.py"
    engine = StrategyEngine([tmp_path / "strategies/custom"])
    save_request = StrategyCodeSaveRequest(strategy_id=sid, code=source.read_text(encoding="utf-8"))
    saved = save_strategy_code_core(save_request, engine, tmp_path)
    monkeypatch.setattr(market_data, "cn_now", lambda: datetime(2026, 10, 20, 16, tzinfo=CN_TZ))
    return engine, sid, pattern, saved, save_request


def test_save_loads_daily_research_without_implicit_entry_gates(registered):
    engine, sid, pattern, saved, _ = registered
    strategy = engine.get(sid)
    assert saved["source"] == "custom"
    assert strategy.execution_backend == "python_history_legacy"
    assert strategy.meta["realtime_supported"] is False
    assert strategy.meta["asset_types"] == ["stock"]
    assert strategy.meta["timeframes"] == ["1d"]
    assert not strategy.meta.get("research_only")
    assert strategy.basic_filter["enabled"] is False
    assert strategy.meta["scoring"] == {}
    assert engine._result_limit(strategy, {}) is None
    assert strategy.entry_signals == strategy.exit_signals == []
    assert strategy.stop_loss is strategy.max_hold_days is None
    params = engine.resolve_params(strategy)
    assert params["lookback_days"] == 35
    assert params["min_history_days"] == 60
    assert params["universe"] == "hs_a_non_st"
    specific = {
        "platform": {"platform_window": 40, "platform_max_range": .25, "platform_near_high": .1},
        "trend": {"trend_window": 40, "trend_min_return": .03, "trend_max_return": .4},
        "oversold": {"oversold_window": 60, "oversold_min_drawdown": .25},
    }[pattern]
    assert set(params) == {"universe", "lookback_days", "min_history_days"} | set(specific)
    assert {key: params[key] for key in specific} == specific


def test_saved_params_change_candidates_and_preserve_workbench_config(registered, tmp_path):
    engine, sid, pattern, _, _ = registered
    mode_path = tmp_path / "user_data/first_board/config.json"
    mode_path.parent.mkdir(parents=True)
    original = b'{"current":{"revision":13,"rules":{"lookback_days":35}}}'
    mode_path.write_bytes(original)
    frame = _history(pattern)
    context = StrategyDataContext("stock", "1d", frame["date"].max(), history=frame)
    assert engine.run(sid, context).total == 1
    changed = {
        "platform": {"platform_max_range": .1},
        "trend": {"trend_min_return": .2},
        "oversold": {"oversold_min_drawdown": .35},
    }[pattern]
    config.save_override(tmp_path, sid, {"params": changed})
    engine.reload()
    saved = config.load_override(tmp_path, sid)
    assert engine.run(sid, context, overrides=saved).total == 0
    detail = _strategy_detail(engine.get(sid), saved)
    for key, value in changed.items():
        assert detail["params_defaults"][key] == value
    assert mode_path.read_bytes() == original


@pytest.mark.parametrize("registered", ["trend"], indirect=True)
@pytest.mark.parametrize("changed", [
    {"trend_window": 10}, {"trend_min_return": .2}, {"trend_max_return": .05},
])
def test_each_trend_parameter_changes_confirmed_candidates(registered, changed):
    engine, sid, pattern, _, _ = registered
    frame = _history(pattern)
    context = StrategyDataContext("stock", "1d", frame["date"].max(), history=frame)
    assert engine.run(sid, context).total == 1
    assert engine.run(sid, context, overrides={"params": changed}).total == 0


@pytest.mark.parametrize("registered", ["trend"], indirect=True)
def test_trend_rejects_below_mean_close_even_when_window_return_qualifies(registered):
    engine, sid, pattern, _, _ = registered
    frame = _history(pattern)
    final_day = frame["date"].max()
    # The final 40-day return remains above 3%, but the close is below its mean.
    frame = frame.with_columns([
        pl.when(pl.col("date") == final_day).then(value).otherwise(pl.col(column)).alias(column)
        for column, value in {
            "open": 11.15, "close": 11.15, "raw_close": 11.15,
            "high": 11.25, "raw_high": 11.25, "low": 11.05, "raw_low": 11.05,
        }.items()
    ])
    closes = frame.tail(40)["close"]
    assert .03 <= closes[-1] / closes[0] - 1 <= .4
    assert closes[-1] < closes.mean()
    assert engine.run(sid, StrategyDataContext("stock", "1d", final_day, history=frame)).total == 0


def test_duplicate_create_preserves_existing_strategy(registered, tmp_path):
    engine, sid, _, _, request = registered
    path = engine.get(sid).file_path
    original = path.read_bytes()
    with pytest.raises(ValueError, match="已存在"):
        save_strategy_code_core(request, engine, tmp_path)
    assert path.read_bytes() == original


@pytest.mark.parametrize(("asset", "timeframe"), [("etf", "1d"), ("index", "1d"), ("stock", "1m")])
def test_daily_strategy_rejects_unsupported_context_before_data_access(registered, asset, timeframe):
    engine, sid, _, _, _ = registered
    with pytest.raises(ValueError):
        engine.run(sid, StrategyDataContext(asset, timeframe, date(2025, 5, 1)))


def test_realtime_monitor_skips_daily_strategy_before_history_loading(registered, monkeypatch):
    from app.strategy.monitor import MonitorRuleEngine

    engine, sid, _, _, _ = registered
    monitor = MonitorRuleEngine()
    monitor._strategy_engine = engine

    def unexpected(*args, **kwargs):
        pytest.fail("Daily research must not read history or evaluate on quote callbacks")

    monkeypatch.setattr(monitor, "_history_loader_for", unexpected)
    monkeypatch.setattr(engine, "run", unexpected)
    assert monitor._match_strategy(pl.DataFrame({"symbol": ["600001.SH"]}), {
        "id": "first_board_daily", "strategy_id": sid, "asset_type": "stock",
    }) == []


def test_declared_history_covers_largest_supported_parameters(registered):
    engine, sid, pattern, _, _ = registered
    params = engine.resolve_params(engine.get(sid))
    params.update(min_history_days=250, lookback_days=60)
    params[f"{pattern}_window"] = 120 if pattern == "oversold" else 60
    rules = FirstBoardRules(**params, enabled_patterns=[pattern])
    from app.strategy.first_board import required_history_bars

    required = engine.required_history_bars([sid], params_map={sid: params})
    assert required >= required_history_bars(rules) == 251
    frame = _history(pattern, 251)
    assert engine.get(sid).filter_history_fn(frame, params).filter(pl.col("date") == frame["date"].max()).height == 1


def test_signal_day_matches_next_session_workbench_pool_without_extra_shift(registered):
    engine, sid, pattern, _, _ = registered
    frame = _history(pattern)
    params = engine.resolve_params(engine.get(sid))
    signal_day = frame["date"].max()
    next_day = _days(81)[-1]
    hits = engine.get(sid).filter_history_fn(frame, params).filter(pl.col("date") == signal_day)
    pool = build_candidates(frame, next_day, FirstBoardRules(**params, enabled_patterns=[pattern]))
    keys = ["symbol", "pattern", "reference_date", "reference_price", "breakout_price"]
    assert hits.height == pool.height == 1
    assert hits.select(keys).equals(pool.select(keys))
    assert hits["reference_date"].to_list() == [signal_day]


def test_future_prices_cannot_change_prior_signals(registered):
    engine, sid, pattern, _, _ = registered
    strategy = engine.get(sid)
    params = engine.resolve_params(strategy)
    full = _history(pattern)
    cutoff = full["date"][-6]
    past = full.filter(pl.col("date") <= cutoff)
    changed = full.with_columns([
        pl.when(pl.col("date") > cutoff).then(pl.col(column) * 3).otherwise(pl.col(column)).alias(column)
        for column in ("open", "high", "low", "close", "raw_close", "raw_high", "raw_low")
    ])
    expected = strategy.filter_history_fn(past, params)
    assert expected.height > 0
    assert strategy.filter_history_fn(changed, params).filter(pl.col("date") <= cutoff).equals(expected)


@pytest.mark.parametrize(("clock", "include_today"), [(time(14, 59, 59), False), (time(15), True)])
def test_bridge_excludes_incomplete_today_and_future_rows(registered, monkeypatch, clock, include_today):
    engine, sid, pattern, _, _ = registered
    frame = _history(pattern)
    today = frame["date"][-2]
    monkeypatch.setattr(market_data, "cn_now", lambda: datetime.combine(today, clock, CN_TZ))
    strategy = engine.get(sid)
    hits = strategy.filter_history_fn(frame, engine.resolve_params(strategy))
    assert hits.height > 0
    assert hits["date"].max() == (today if include_today else frame["date"][-3])
    assert not hits.filter(pl.col("date") > today).height


@pytest.mark.parametrize("params", [{"min_change_pct": .05}, {"min_amount": 1}, {"unknown": 1}])
def test_daily_helper_rejects_parameters_that_cannot_affect_research(params):
    with pytest.raises(ValueError, match="不支持这些参数"):
        filter_daily_pattern_history(_history("platform"), params, "platform")


def test_daily_helper_rejects_other_pattern_parameters_and_invalid_ranges():
    with pytest.raises(ValueError, match="不支持这些参数"):
        filter_daily_pattern_history(_history("platform"), {"oversold_window": 60}, "platform")
    with pytest.raises(ValueError):
        filter_daily_pattern_history(_history("platform"), {"platform_window": 0}, "platform")
    with pytest.raises(ValueError, match="不支持的"):
        filter_daily_pattern_history(_history("platform"), {}, "not_a_pattern")
    with pytest.raises(ValueError, match="不支持这些参数"):
        filter_daily_pattern_history(_history("trend"), {"platform_window": 40}, "trend")
    with pytest.raises(ValueError, match="趋势涨幅下限不能大于上限"):
        filter_daily_pattern_history(_history("trend"), {
            "trend_min_return": .4, "trend_max_return": .03,
        }, "trend")


def test_daily_helper_preserves_missing_price_and_duplicate_failures():
    frame = _history("platform")
    for column in ("raw_close", "limit_up_price"):
        with pytest.raises(ValueError, match=column):
            filter_daily_pattern_history(frame.drop(column), {}, "platform")
    with pytest.raises(ValueError, match="重复"):
        filter_daily_pattern_history(pl.concat([frame, frame.tail(1)]), {}, "platform")


def test_daily_helper_does_not_replace_explicit_missing_limit_evidence():
    frame = _history("platform")
    params = {"platform_max_range": .25, "platform_near_high": .1}
    assert filter_daily_pattern_history(frame, params, "platform").height > 0
    unavailable = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias("limit_up_price"))
    assert filter_daily_pattern_history(unavailable, params, "platform").is_empty()


@pytest.mark.parametrize("edited_limit_signals", [False, True])
def test_normal_feature_pipeline_produces_authoritative_limit_evidence(
    registered, monkeypatch, tmp_path, edited_limit_signals,
):
    from app.indicators import pipeline
    from tests.signal_seeds import install_pipeline_caches

    install_pipeline_caches(monkeypatch)
    if edited_limit_signals:
        # Editable signals need not reference exchange limit prices. The strategy
        # still requires those prices independently of the user's signal formulas.
        monkeypatch.setattr(pipeline, "_custom_signal_exprs", {
            "signal_limit_up": pl.lit(False), "signal_limit_down": pl.lit(False),
        })
    engine, sid, pattern, _, _ = registered
    frame = _history(pattern).drop("name", "signal_limit_up", "signal_limit_down", "limit_up_price")
    monkeypatch.setattr("app.backtest.engine.pl.scan_parquet", lambda *args, **kwargs: frame.lazy())
    instruments = pl.DataFrame({
        "symbol": ["600001.SH"], "name": ["研究样本"], "listing_date": [date(2000, 1, 1)],
    })
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path), get_enriched_range=lambda *args, **kwargs: None,
        get_instruments_asset=lambda asset: instruments, get_historical_shares=lambda: pl.DataFrame(),
    )
    strategy = engine.get(sid)
    params = engine.resolve_params(strategy)
    plan = StrategyDependencyResolver().resolve(
        strategy, params=params, basic_filter={"enabled": False}, entry_signals=[], exit_signals=[],
    )
    assert "limit_up_price" in plan.signal_columns
    loaded = BacktestEngine(repo).load_panel_for_backtest(
        ["600001.SH"], frame["date"].min(), frame["date"].max(), plan,
    )
    assert "limit_up_price" in loaded.columns
    assert loaded["limit_up_price"][-1] is not None
    hits = strategy.filter_history_fn(loaded, params)
    assert hits.filter(pl.col("date") == frame["date"].max()).height == 1


def test_generic_backtest_fills_next_open_once_after_signal_day(registered, monkeypatch, tmp_path):
    from tests.signal_seeds import install_pipeline_caches

    install_pipeline_caches(monkeypatch)
    engine, sid, pattern, _, _ = registered
    frame = _history(pattern)
    signal_day, fill_day = frame["date"][-3], frame["date"][-2]
    frame = frame.with_columns(pl.when(pl.col("date") == fill_day).then(pl.col("close") + .05)
                              .otherwise(pl.col("open")).alias("open"))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           get_index_daily=lambda *args, **kwargs: pl.DataFrame())
    backtest_engine = BacktestEngine(repo)
    monkeypatch.setattr(backtest_engine, "load_panel_for_backtest", lambda *args, **kwargs: frame)
    service = StrategyBacktestService(backtest_engine, engine)
    result = service.run(StrategyBacktestConfig(
        strategy_id=sid, symbols=None, start=signal_day, end=signal_day, mode="full",
        holding_days=1, commission_pct=0, stamp_tax_pct=0, slippage_bps=0,
    ))
    assert result.error is None
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade["entry_signal_date"] == str(signal_day)
    assert trade["entry_date"] == str(fill_day)
    expected_price = frame.filter(pl.col("date") == fill_day)["open"].item()
    assert trade["entry_price"] == pytest.approx(expected_price, abs=.0001)
