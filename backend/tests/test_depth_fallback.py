"""depth5 兜底链: 一轮结果只来自一个源, 主源整轮失败时同轮换源, 部分失败不换源。"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest

from app.data_providers import routing_health as rh
from app.services import depth_service as depth_module
from app.services.depth_service import DepthService
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


def _ok(symbols):
    return {s: {"ask_volumes": [0], "bid_volumes": [5]} for s in symbols}


def _boom(symbols):
    raise RuntimeError("down")


@pytest.fixture
def env(monkeypatch):
    rh.reset()
    state = SimpleNamespace(
        chain=["tdxquant", "meoz"],
        providers={"tdxquant": SimpleNamespace(get_depth_batch=MagicMock(side_effect=_ok)),
                   "meoz": SimpleNamespace(get_depth_batch=MagicMock(side_effect=_ok))},
    )
    monkeypatch.setattr(rh, "_primary", lambda cap_id: state.chain[0])
    monkeypatch.setattr(rh, "_fallbacks", lambda cap_id: state.chain[1:])
    monkeypatch.setattr(rh, "_candidate_names", lambda cap_id: set(state.providers))
    monkeypatch.setattr(rh, "resolve_provider", lambda name: state.providers[name])
    monkeypatch.setattr(depth_module, "sleep_between_batches", MagicMock())
    monkeypatch.setattr(depth_module.time, "sleep", lambda s: None)
    yield state
    rh.reset()


def _service(batch: int = 2) -> DepthService:
    service = DepthService()
    service._app_state = SimpleNamespace(capabilities=CapabilitySet({
        Cap.DEPTH5_BATCH: CapabilityLimits(batch=batch, rpm=600),
    }))
    return service


def _sources(result: dict) -> set:
    return {entry["source"] for entry in result.values()}


def test_primary_ok_entries_carry_source(env):
    result = _service()._call_depth_batch(["A", "B", "C"])
    assert set(result) == {"A", "B", "C"}
    assert _sources(result) == {"tdxquant"}
    env.providers["meoz"].get_depth_batch.assert_not_called()


def test_primary_raises_all_batches_switches_same_round(env):
    env.providers["tdxquant"].get_depth_batch.side_effect = _boom
    result = _service()._call_depth_batch(["A", "B", "C"])
    assert set(result) == {"A", "B", "C"}
    assert _sources(result) == {"meoz"}
    assert env.providers["tdxquant"].get_depth_batch.call_count == 2   # 两批都试过


def test_primary_returns_empty_switches(env):
    # tdxquant 凭据失效时吞掉批次异常, 整轮返回空 dict
    env.providers["tdxquant"].get_depth_batch.side_effect = lambda symbols: {}
    result = _service()._call_depth_batch(["A", "B"])
    assert _sources(result) == {"meoz"}


def test_partial_failure_keeps_primary_no_mixing(env):
    env.providers["tdxquant"].get_depth_batch.side_effect = [_ok(["A", "B"]), RuntimeError("x"), _ok(["E"])]
    result = _service()._call_depth_batch(["A", "B", "C", "D", "E"])
    assert set(result) == {"A", "B", "E"}
    assert _sources(result) == {"tdxquant"}
    env.providers["meoz"].get_depth_batch.assert_not_called()


def test_all_sources_fail_returns_empty(env):
    for p in env.providers.values():
        p.get_depth_batch.side_effect = _boom
    assert _service()._call_depth_batch(["A"]) == {}


def test_breaker_opened_primary_skipped_next_round(env):
    env.providers["tdxquant"].get_depth_batch.side_effect = _boom
    service = _service()
    for _ in range(3):
        service._call_depth_batch(["A"])
    calls = env.providers["tdxquant"].get_depth_batch.call_count
    assert _sources(service._call_depth_batch(["A"])) == {"meoz"}
    assert env.providers["tdxquant"].get_depth_batch.call_count == calls


def test_unresolvable_source_skipped(env, monkeypatch):
    def resolve(name):
        if name == "tdxquant":
            raise LookupError(name)
        return env.providers[name]

    monkeypatch.setattr(rh, "resolve_provider", resolve)
    assert _sources(_service()._call_depth_batch(["A"])) == {"meoz"}
    assert rh.chain_status("depth5")[0]["state"] == "healthy"


def test_order_books_entries_carry_source(env, monkeypatch):
    service = _service()
    monkeypatch.setattr(service, "_has_capability", lambda: True)
    env.providers["tdxquant"].get_depth_batch.side_effect = _boom
    books = service.get_order_books(["A", "B"])
    assert _sources(books) == {"meoz"}


def test_seal_and_persist_write_source_column(env, tmp_path):
    service = _service()
    service._repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    depth = service._call_depth_batch(["A", "B"])
    day = date(2026, 11, 18)
    with service._fetch_lock:
        service._seal_locked(depth, ["A", "B"], [], day, persist=True)
    assert service._sealed_source == "tdxquant"
    df = pl.read_parquet(tmp_path / "depth5" / "date=2026-11-18" / "part.parquet")
    assert df["source"].to_list() == ["tdxquant", "tdxquant"]
    restored = _service()
    restored._repo = service._repo
    restored._restore_from_parquet(day)
    assert restored._sealed_cache["A"]["source"] == "tdxquant"


def test_restore_old_parquet_without_source_column(env, tmp_path):
    out = tmp_path / "depth5" / "date=2026-11-17" / "part.parquet"
    out.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["A"], "sealed_up": [True], "sealed_down": [None], "ask1_vol": [0],
                  "bid1_vol": [9], "status": ["limit_up"], "fetched_at": [1.0]},
                 schema_overrides={"sealed_down": pl.Boolean}).write_parquet(out)
    service = _service()
    service._repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    service._restore_from_parquet(date(2026, 11, 17))
    assert service._sealed_cache["A"]["source"] is None
