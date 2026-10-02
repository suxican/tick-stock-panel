"""历史扩展不得因非空但不完整的全 A 池漏掉本地股票。"""
from __future__ import annotations

from types import SimpleNamespace

import polars as pl
import pytest

from app.config import settings
from app.services.extend_history import _resolve_universe
from app.tickflow import pools


def _write_instruments(root, directory, data):
    path = root / directory / f"{directory}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(data).write_parquet(path)


@pytest.fixture
def universe_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(pools, "DEMO_SYMBOLS", ["000001.SZ"])
    state = {"all_a": ["000001.SZ"], "watchlist": [], "calls": []}

    def get_pool(name, refresh=False):
        state["calls"].append((name, refresh))
        value = state["all_a" if name == "CN_Equity_A" else "watchlist"]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(pools, "get_pool", get_pool)
    return state, tmp_path


def test_nonempty_pool_is_supplemented_by_local_stocks(universe_env):
    state, root = universe_env
    _write_instruments(root, "instruments", {
        "symbol": ["000001.SZ", "603291.SH", "001222.SZ"],
    })
    state["watchlist"] = ["600519.SH"]

    result = _resolve_universe(SimpleNamespace(has=lambda cap: True))

    assert result == ["000001.SZ", "001222.SZ", "600519.SH", "603291.SH"]
    assert ("CN_Equity_A", True) in state["calls"]


@pytest.mark.parametrize("type_column", ["asset_type", "type"])
def test_assets_are_filtered_and_symbols_normalized(universe_env, type_column):
    state, root = universe_env
    state["all_a"] = ["000001.SZ", "603291.sh", "", None]
    state["watchlist"] = [" 603291.SH ", "510300.SH", "000001.SH", "600519", "BAD"]
    _write_instruments(root, "instruments", {
        "symbol": ["001222.SZ", "001222.SZ", "688001.SH", "510500.SH", "399001.SZ", None],
        type_column: ["stock", "stock", None, "etf", "index", "stock"],
    })
    _write_instruments(root, "instruments_etf", {"symbol": ["510300.SH"]})
    _write_instruments(root, "instruments_index", {"symbol": ["000001.SH"]})

    result = _resolve_universe(SimpleNamespace(has=lambda cap: True))

    assert result == ["000001.SZ", "001222.SZ", "603291.SH", "688001.SH"]


@pytest.mark.parametrize("remote", [[], RuntimeError("pool unavailable")])
def test_empty_or_failed_pool_keeps_local_stocks(universe_env, remote):
    state, root = universe_env
    state["all_a"] = remote
    _write_instruments(root, "instruments", {"symbol": ["603291.SH"]})

    assert _resolve_universe(SimpleNamespace(has=lambda cap: True)) == [
        "000001.SZ", "603291.SH",
    ]


def test_no_batch_capability_keeps_local_stocks_without_remote_call(universe_env):
    state, root = universe_env
    state["all_a"] = AssertionError("must not fetch full pool")
    _write_instruments(root, "instruments", {"symbol": ["920001.BJ", "603291.SH"]})

    assert _resolve_universe(SimpleNamespace(has=lambda cap: False)) == [
        "000001.SZ", "603291.SH", "920001.BJ",
    ]
    assert all(name != "CN_Equity_A" for name, _ in state["calls"])


@pytest.mark.parametrize("bad_local", ["missing_symbol", "corrupt"])
def test_unreadable_local_instruments_preserve_provider_pool(universe_env, bad_local):
    _, root = universe_env
    if bad_local == "missing_symbol":
        _write_instruments(root, "instruments", {"name": ["unknown"]})
    else:
        path = root / "instruments" / "instruments.parquet"
        path.parent.mkdir(parents=True)
        path.write_text("not parquet", encoding="utf-8")

    assert _resolve_universe(SimpleNamespace(has=lambda cap: True)) == ["000001.SZ"]


def test_corrupt_watchlist_does_not_hide_local_stocks(universe_env):
    state, root = universe_env
    state["watchlist"] = ValueError("watchlist corrupt")
    _write_instruments(root, "instruments", {"symbol": ["603291.SH"]})

    assert _resolve_universe(SimpleNamespace(has=lambda cap: True)) == [
        "000001.SZ", "603291.SH",
    ]
