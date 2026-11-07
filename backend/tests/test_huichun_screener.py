"""The strategy adapter uses the same causal A0 history as the dedicated mode."""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from app.market_time import CN_TZ
from app.services import huichun_mode, huichun_screener
from app.strategy import market_data
from app.strategy.engine import StrategyDataContext, StrategyEngine
from app.tickflow import repository as repo_module
from app.tickflow.repository import DataStore

PARAMS = {"ma_window": 3, "slope_lag": 1, "zero_threshold": 1.0}


@pytest.fixture
def history(tmp_path, monkeypatch):
    prices = (
        [10.0] * 260
        + [10 - i * 0.1 for i in range(1, 21)]
        + [8 + i * 0.4 for i in range(1, 36)]
        + [22 - i * 0.1 for i in range(1, 61)]
        + [16 + i * 0.25 for i in range(1, 31)]
    )
    days = []
    day = date(2024, 1, 2)
    while len(days) < len(prices):
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    store = DataStore(tmp_path / "data")
    raw = pl.DataFrame({
        "symbol": ["600001.SH"] * len(days), "date": days,
        **{key: prices for key in ("open", "high", "low", "close")},
        "volume": [1000.] * len(days), "amount": [10000.] * len(days),
        "daily_batch_finalized": [True] * len(days),
    })
    raw_path = store.data_dir / "kline_daily" / "fixture.parquet"
    raw.write_parquet(raw_path)
    calendar = store.data_dir / "user_data" / "huichun" / "market_calendar.json"
    calendar.parent.mkdir(parents=True)
    calendar.write_text(json.dumps({
        "version": 1, "coverage_start": str(days[0]), "coverage_end": "2026-12-31",
        "weekends_closed": True, "closures": [],
        "sources": [{"id": "test", "url": "https://www.sse.com.cn/test",
                     "published_on": "2023-12-01", "title": "Synthetic calendar"}],
    }), encoding="utf-8")
    store._register_views()
    monkeypatch.setattr(repo_module, "DataStore", lambda: DataStore(store.data_dir))
    market_data._reset_repo()
    repo = market_data._get_repo()
    now = datetime.combine(days[-1], datetime.min.time(), CN_TZ).replace(hour=16)
    monkeypatch.setattr(huichun_screener, "cn_now", lambda: now)
    monkeypatch.setattr(huichun_mode, "_now", lambda: now)
    huichun_screener._clear_cache()
    panel = raw.select("symbol", "date", "close").with_columns(
        pl.lit(123.0).alias("macd_dif"), pl.lit("样本").alias("name"),
    )
    env = SimpleNamespace(repo=repo, store=repo.store, raw=raw, raw_path=raw_path,
                          panel=panel, days=days, now=now)
    yield env
    market_data._reset_repo()
    huichun_screener._clear_cache()
    env.store.db.close()
    store.db.close()


def run(env, params=None, frame=None):
    return huichun_screener.filter_huichun_a0(
        env.repo, env.panel if frame is None else frame, PARAMS if params is None else params,
    )


def replace_raw(env, frame):
    pending = env.raw_path.with_suffix(".tmp")
    frame.write_parquet(pending)
    pending.replace(env.raw_path)


def test_same_candidates_as_original_mode_with_independent_rules(history):
    env = history
    mode = huichun_mode.HuichunModeService(env.repo)
    try:
        initial = mode.get_config()
        mode.save_config(PARAMS, expected_revision=initial["revision"])
        mode.start_scan(env.days[0], env.days[-1])
        snapshot = mode.wait(timeout=15)
        assert snapshot["job"]["status"] == "completed", snapshot
        result = run(env)
        expected = {(r["symbol"], r["signal_date"]) for r in snapshot["scan"]["candidates"]}
        assert expected
        assert {(s, str(d)) for s, d in result.select("symbol", "date").iter_rows()} == expected
        assert result.columns == env.panel.columns
        assert result["macd_dif"].to_list() == [123.] * result.height
        before = mode.get_config()
        assert run(env, {**PARAMS, "rally_threshold": 10.0}).is_empty()
        assert mode.get_config() == before
    finally:
        mode.close(timeout=10)


def test_one_row_input_still_uses_complete_repository_history(history):
    env = history
    all_hits = run(env)
    assert all_hits.height > 0
    assert_frame_equal(run(env, frame=all_hits.tail(1)), all_hits.tail(1))


def test_missing_session_resets_cycle_instead_of_bridging_gap(history):
    env = history
    assert not run(env).is_empty()
    replace_raw(env, env.raw.filter(pl.col("date") != env.days[320]))
    assert run(env).is_empty()


def test_invalid_factor_isolated_and_cache_invalidated(history):
    env = history
    assert not run(env).is_empty()
    pl.DataFrame({"symbol": ["600001.SH"], "trade_date": [env.days[320]],
                  "ex_factor": [0.0]}).write_parquet(
        env.store.data_dir / "adj_factor" / "fixture.parquet",
    )
    assert run(env).is_empty()


def test_removed_factor_files_do_not_read_stale_optional_view(history):
    env = history
    original = run(env)
    path = env.store.data_dir / "adj_factor" / "new.parquet"
    pl.DataFrame({"symbol": ["600001.SH"], "trade_date": [env.days[320]],
                  "ex_factor": [0.0]}).write_parquet(path)
    assert run(env).is_empty()
    path.unlink()
    huichun_screener._clear_cache()
    assert_frame_equal(run(env), original)


def test_repaired_factor_file_is_used_by_the_same_private_repository(history):
    env = history
    original = run(env)
    path = env.store.data_dir / "adj_factor" / "new.parquet"
    factor = pl.DataFrame({"symbol": ["600001.SH"], "trade_date": [env.days[320]],
                           "ex_factor": [0.0]})
    factor.write_parquet(path)
    assert run(env).is_empty()
    pending = path.with_suffix(".tmp")
    factor.with_columns(pl.lit(1.0).alias("ex_factor")).write_parquet(pending)
    pending.replace(path)
    assert_frame_equal(run(env), original)
    assert market_data._get_repo() is env.repo


def test_private_repository_disables_file_cache_for_independent_cursors(history):
    assert history.repo.execute_all(
        "SELECT value FROM duckdb_settings() WHERE name='enable_external_file_cache'",
    ) == [("false",)]


def test_added_listing_table_is_refreshed_before_filtering(history):
    env = history
    assert not run(env).is_empty()
    pl.DataFrame({"symbol": ["600001.SH"], "listing_date": [env.days[300]]}).write_parquet(
        env.store.data_dir / "instruments" / "new.parquet",
    )
    assert run(env).is_empty()


def test_removed_raw_files_do_not_read_stale_daily_view(history):
    env = history
    assert not run(env).is_empty()
    env.raw_path.unlink()
    assert run(env).is_empty()


def test_view_refresh_failure_rejects_result(history, monkeypatch):
    monkeypatch.setattr(history.repo, "refresh_view", lambda *_: False)
    with pytest.raises(ValueError, match="读取视图"):
        run(history)


def test_future_prices_and_factors_do_not_change_past_signals(history):
    env = history
    hit = run(env).tail(1)
    end = hit["date"].item()
    frame = env.panel.filter(pl.col("date") <= end)
    before = run(env, frame=frame)
    replace_raw(env, env.raw.with_columns([
        pl.when(pl.col("date") > end).then(pl.col(key) * 100).otherwise(pl.col(key)).alias(key)
        for key in ("open", "high", "low", "close")
    ]))
    pl.DataFrame({"symbol": ["600001.SH"], "trade_date": [env.days[-1]],
                  "ex_factor": [0.0]}).write_parquet(
        env.store.data_dir / "adj_factor" / "fixture.parquet",
    )
    env.store._register_views()
    assert_frame_equal(run(env, frame=frame), before)


def test_empty_and_unsupported_assets_do_not_read_repository():
    empty = pl.DataFrame(schema={"symbol": pl.String, "date": pl.Date})
    assert huichun_screener.filter_huichun_a0(None, empty, {}).is_empty()
    other = pl.DataFrame({"symbol": ["510300.SH", "899050.BJ", "000001.SH"],
                          "date": [date(2024, 1, 2)] * 3})
    assert huichun_screener.filter_huichun_a0(None, other, {}).is_empty()


def test_unclosed_day_excluded_and_after_close_matches_original_candidates(history, monkeypatch):
    env = history
    hit = run(env).tail(1)
    day = hit["date"].item()
    current = datetime.combine(day, datetime.min.time(), CN_TZ).replace(hour=14)
    monkeypatch.setattr(huichun_screener, "cn_now", lambda: current)
    assert run(env, frame=hit).is_empty()
    current = current.replace(hour=16)
    assert_frame_equal(run(env, frame=hit), hit)
    replace_raw(env, env.raw.with_columns(pl.lit(False).alias("daily_batch_finalized")))
    assert_frame_equal(run(env, frame=hit), hit)


def test_cache_reuses_keys_but_preserves_latest_input_columns(history, monkeypatch):
    env = history
    calls = []
    audit = huichun_screener.audit_a0
    def observed(*args, **kwargs):
        calls.append(1)
        return audit(*args, **kwargs)
    monkeypatch.setattr(huichun_screener, "audit_a0", observed)
    first = run(env)
    second = run(env, frame=env.panel.with_columns(pl.lit(456.).alias("macd_dif")))
    assert len(calls) == 1
    assert second.height == first.height > 0
    assert second["macd_dif"].to_list() == [456.] * second.height


def test_input_changed_during_scan_is_not_cached(history, monkeypatch):
    env = history
    original = huichun_screener.HuichunDataReader.input_signature
    calls = []
    def changed(reader, cutoff=None):
        calls.append(1)
        return [*original(reader, cutoff), ("changed", len(calls))]
    monkeypatch.setattr(huichun_screener.HuichunDataReader, "input_signature", changed)
    with pytest.raises(ValueError, match="发生变化"):
        run(env)
    assert not huichun_screener._CACHE


@pytest.mark.parametrize("params", [{"warmup_bars": 0}, {"ma_window": 1},
                                    {"zero_threshold": float("nan")}])
def test_invalid_parameters_fail_explicitly(history, params):
    with pytest.raises(ValueError):
        run(history, params)


def test_strategy_screening_and_backtest_candidate_mask_agree(history, tmp_path):
    from app.backtest.strategy import StrategyBacktestService

    env = history
    directory = tmp_path / "strategies"
    directory.mkdir()
    fixture = Path(__file__).parent / "fixtures" / "strategies" / "custom_huichun_a0.py"
    (directory / fixture.name).write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    engine = StrategyEngine([directory])
    strategy = engine.get("custom_huichun_a0")
    hits = run(env)
    day = hits["date"].max()
    current = env.panel.filter(pl.col("date") == day)
    result = engine.run("custom_huichun_a0", StrategyDataContext(
        as_of=day, asset_type="stock", timeframe="1d", current=current, history=current,
    ), params=PARAMS)
    assert {r["symbol"] for r in result.rows} == set(hits.filter(pl.col("date") == day)["symbol"])
    mask = StrategyBacktestService._build_candidate_filter_mask(None, env.panel, strategy, PARAMS)
    assert_frame_equal(env.panel.filter(mask), hits)


@pytest.mark.parametrize("reverse_input", [False, True])
def test_complete_backtest_orders_a0_candidates_and_respects_fill_dates(
    history, tmp_path, monkeypatch, reverse_input,
):
    from app.backtest.engine import BacktestEngine
    from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
    from tests.signal_seeds import install_pipeline_caches

    env = history
    install_pipeline_caches(monkeypatch)
    # Two identical A0 patterns compete for one position. Distinct open/close
    # prices make an accidental signal-close fill observable in the trade record.
    raw = pl.concat([
        env.raw,
        env.raw.with_columns(pl.lit("600002.SH").alias("symbol")),
    ]).with_columns(
        (pl.col("close") * .995).alias("open"),
        (pl.col("close") * .995).alias("low"),
    )
    replace_raw(env, raw)
    enriched = raw.with_columns(
        pl.col("close").alias("raw_close"),
        pl.col("high").alias("raw_high"),
        pl.col("low").alias("raw_low"),
    )
    enriched.write_parquet(env.store.data_dir / "kline_daily_enriched" / "fixture.parquet")
    pl.DataFrame({
        "symbol": ["600001.SH", "600002.SH"], "name": ["样本一", "样本二"],
        "total_shares": [1e8, 1e8], "float_shares": [1e8, 1e8],
    }).write_parquet(env.store.data_dir / "instruments" / "fixture.parquet")
    hits = run(env, frame=raw)
    assert set(hits["symbol"]) == {"600001.SH", "600002.SH"}
    signal_day = hits["date"].max()
    signal_index = env.days.index(signal_day)
    entry_day, earliest_exit_day = env.days[signal_index + 1:signal_index + 3]

    directory = tmp_path / "backtest-strategy"
    directory.mkdir()
    fixture = Path(__file__).parent / "fixtures" / "strategies" / "custom_huichun_a0.py"
    (directory / fixture.name).write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    engine = BacktestEngine(env.repo)
    if reverse_input:
        load_panel = engine.load_panel_for_backtest
        def reversed_panel(*args, **kwargs):
            return load_panel(*args, **kwargs).reverse()
        monkeypatch.setattr(engine, "load_panel_for_backtest", reversed_panel)
    service = StrategyBacktestService(engine, StrategyEngine([directory]))
    result = service.run(StrategyBacktestConfig(
        strategy_id="custom_huichun_a0", symbols=None,
        start=signal_day, end=env.days[signal_index + 5], params=PARAMS,
        matching="open_t+1", max_positions=1, initial_capital=100_000.,
        fees_pct=0., slippage_bps=0., overrides={"max_hold_days": 1},
    ))

    assert result.error is None, result.error
    assert result.stats["selection"]["strategy_matches"] == 2
    assert result.stats["selection"]["entry_candidates"] == 2
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade["symbol"] == "600001.SH"
    assert trade["entry_signal_date"] == str(signal_day)
    assert trade["entry_date"] == str(entry_day)
    expected_open = raw.filter(
        (pl.col("symbol") == trade["symbol"]) & (pl.col("date") == entry_day),
    )["open"].item()
    assert trade["entry_price"] == pytest.approx(expected_open, abs=1e-5)
    assert trade["exit_date"] >= str(earliest_exit_day)
    assert trade["duration"] >= 1
    assert trade["exit_reason"] == "max_hold"
