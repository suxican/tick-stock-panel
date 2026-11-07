"""A0 uses the existing custom strategy save/parameter contracts."""

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.api.strategy import (
    StrategyCodeSaveRequest,
    _strategy_detail,
    save_strategy_code_core,
)
from app.strategy import config
from app.strategy.engine import StrategyDataContext, StrategyEngine

SOURCE = Path(__file__).parent / "fixtures/strategies/custom_huichun_a0.py"
SID = "custom_huichun_a0"


@pytest.fixture
def registered(tmp_path):
    engine = StrategyEngine([tmp_path / "strategies/custom"])
    request = StrategyCodeSaveRequest(strategy_id=SID, code=SOURCE.read_text(encoding="utf-8"))
    saved = save_strategy_code_core(request, engine, tmp_path)
    return engine, saved, request


def test_save_discovers_a0_without_extra_filters_or_truncation(registered):
    engine, saved, _ = registered
    strategy = engine.get(SID)
    assert saved["source"] == "custom"
    assert strategy.execution_backend == "python_history_legacy"
    assert not strategy.meta.get("research_only")
    assert strategy.basic_filter["enabled"] is False
    assert engine._result_limit(strategy, {}) is None
    assert engine.required_history_bars([SID]) == 2
    assert strategy.entry_signals == strategy.exit_signals == []
    assert strategy.stop_loss is strategy.max_hold_days is None


def test_parameters_persist_independently_and_survive_reload(registered, tmp_path):
    engine, _, _ = registered
    old_mode = tmp_path / "user_data/huichun/config.json"
    old_mode.parent.mkdir(parents=True)
    original = '{"current":{"revision":7,"rules":{"rally_threshold":0.4}}}'
    old_mode.write_text(original, encoding="utf-8")
    overrides = {"params": {"rally_threshold": 0.6, "zero_threshold": 0.01, "ma_window": 80}}
    config.save_override(tmp_path, SID, overrides)
    engine.reload()
    strategy = engine.get(SID)
    saved = config.load_override(tmp_path, SID)
    detail = _strategy_detail(strategy, saved)
    assert detail["params_defaults"]["rally_threshold"] == 0.6
    assert detail["params_defaults"]["zero_threshold"] == 0.01
    assert detail["params_defaults"]["ma_window"] == 80
    assert detail["params_defaults"]["warmup_bars"] == 250
    assert engine.resolve_params(strategy, None, saved) == detail["params_defaults"]
    assert old_mode.read_text(encoding="utf-8") == original


def test_duplicate_create_preserves_existing_strategy(registered, tmp_path):
    engine, _, request = registered
    path = engine.get(SID).file_path
    original = path.read_bytes()
    with pytest.raises(ValueError, match="已存在"):
        save_strategy_code_core(request, engine, tmp_path)
    assert path.read_bytes() == original


@pytest.mark.parametrize(("asset", "timeframe"), [("etf", "1d"), ("stock", "1m")])
def test_a0_rejects_unsupported_context_before_data_access(registered, asset, timeframe):
    engine, _, _ = registered
    with pytest.raises(ValueError):
        engine.run(SID, StrategyDataContext(asset, timeframe, date(2026, 10, 20)))


def test_market_data_bridge_uses_configured_repository(monkeypatch):
    from app.services import huichun_screener
    from app.strategy import market_data

    repo, frame, params = SimpleNamespace(), object(), {"ma_window": 80}
    expected = object()
    monkeypatch.setattr(market_data, "_get_repo", lambda: repo)

    def screen(actual_repo, actual_frame, actual_params):
        assert actual_repo is repo
        assert actual_frame is frame
        assert actual_params is params
        return expected

    monkeypatch.setattr(huichun_screener, "filter_huichun_a0", screen)
    assert market_data.filter_huichun_a0(frame, params) is expected


def test_realtime_monitor_skips_a0_before_loading_any_history(registered, monkeypatch):
    from app.strategy.monitor import MonitorRuleEngine

    engine, _, _ = registered
    monitor = MonitorRuleEngine()
    monitor._strategy_engine = engine

    def unexpected(*args, **kwargs):
        pytest.fail("A0 must not load history or compute on the quote callback thread")

    monkeypatch.setattr(monitor, "_history_loader_for", unexpected)
    monkeypatch.setattr(engine, "run", unexpected)
    assert monitor._match_strategy(pl.DataFrame({"symbol": ["600001.SH"]}), {
        "id": "a0", "strategy_id": SID, "asset_type": "stock",
    }) == []


def test_saved_realtime_rule_explains_daily_only_status(registered, tmp_path):
    from app.api import monitor_rules as api
    from app.strategy import monitor_rules

    engine, _, _ = registered
    monitor_rules.save_one(tmp_path, {
        "id": "a0", "name": "A0", "type": "strategy", "strategy_id": SID,
        "scope": "all", "asset_type": "stock", "enabled": True,
    })
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), strategy_engine=engine,
    )))
    rules = api.list_rules(request)["rules"]
    assert rules[0]["runtime_warning"] == "该策略仅支持已收盘日线筛选，不执行实时监控"
