"""兜底链终审问题的回归测试 (每条对应审查报告中的一个 Important 问题)。"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.data_providers import routing_health as rh
from app.data_providers.errors import ProviderAuthError
from app.market_time import CN_TZ
from app.services import depth_service as depth_module
from app.services import market_game_overseas, overseas_indices
from app.services.depth_service import DepthService
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

T0 = datetime(2026, 11, 18, 10, 0, tzinfo=CN_TZ)


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def env(monkeypatch):
    rh.reset()
    overseas_indices._work.clear()
    clock = _Clock()
    state = SimpleNamespace(chain=["tdxquant", "meoz"], providers={}, clock=clock)
    monkeypatch.setattr(rh, "cn_now", clock)
    monkeypatch.setattr(rh, "_primary", lambda cap_id: state.chain[0])
    monkeypatch.setattr(rh, "_fallbacks", lambda cap_id: state.chain[1:])
    monkeypatch.setattr(rh, "_candidate_names", lambda cap_id: set(state.providers))
    monkeypatch.setattr(rh, "resolve_provider", lambda name: state.providers[name])
    monkeypatch.setattr(rh, "_display", lambda name: name)
    monkeypatch.setattr(depth_module, "sleep_between_batches", MagicMock())
    monkeypatch.setattr(depth_module.time, "sleep", lambda s: None)
    yield state
    rh.reset()


def _depth_service() -> DepthService:
    service = DepthService()
    service._app_state = SimpleNamespace(capabilities=CapabilitySet({
        Cap.DEPTH5_BATCH: CapabilityLimits(batch=10, rpm=600),
    }))
    return service


def _ok(symbols):
    return {s: {"ask_volumes": [0]} for s in symbols}


def _boom(symbols):
    raise RuntimeError("down")


# ---- 问题 1: SharedWork 的本地拒绝 / 并发等待者不得重复计入源失败

def test_shared_work_rejections_do_not_count_as_source_failures(env):
    env.chain = ["yahoo_overseas", "cn_overseas"]
    yahoo = SimpleNamespace(get_overseas_index_quotes=MagicMock(side_effect=TimeoutError))
    cn = SimpleNamespace(get_overseas_index_quotes=MagicMock(return_value=[]))
    env.providers = {"yahoo_overseas": yahoo, "cn_overseas": cn}
    for _ in range(3):  # 失败后 1 秒内同键请求被 SharedWork 本地拒绝, 未真正调用上游
        overseas_indices.get_quotes()
    assert yahoo.get_overseas_index_quotes.call_count == 1
    assert rh.chain_status("overseas")[0]["state"] == "healthy"
    assert "连续 1 次" in rh.chain_status("overseas")[0]["reason"]


def test_invoke_that_did_not_run_the_call_is_not_recorded(env):
    env.providers = {"tdxquant": SimpleNamespace(fetch=MagicMock()), "meoz": SimpleNamespace(fetch=MagicMock())}

    def invoke(source, call):
        raise RuntimeError("local backpressure")  # 没有执行 call

    for _ in range(5):
        r = rh.call_with_fallback("depth5", "fetch", invoke=invoke)
    assert (r.source, r.data) == (None, None)
    assert {s["state"] for s in rh.chain_status("depth5")} == {"healthy"}
    assert all(s["reason"] is None for s in rh.chain_status("depth5"))


# ---- 问题 2: 半开探测权不得泄漏

def _open_primary(env):
    env.providers["tdxquant"] = SimpleNamespace(get_depth_batch=MagicMock(side_effect=_boom))
    env.providers["meoz"] = SimpleNamespace(get_depth_batch=MagicMock(side_effect=_ok))
    service = _depth_service()
    for _ in range(3):
        service._call_depth_batch(["A"])
    env.clock.advance(301)
    return service


def test_probe_released_when_round_setup_raises(env, monkeypatch):
    service = _open_primary(env)
    monkeypatch.setattr(service, "_get_capset", MagicMock(side_effect=RuntimeError("capset")))
    with pytest.raises(RuntimeError):
        service._call_depth_batch(["A"])
    assert rh.pick_source("depth5") == "tdxquant"


def test_validate_exception_counts_as_data_failure_without_leak(env):
    env.providers = {"tdxquant": SimpleNamespace(fetch=MagicMock(return_value=1)),
                     "meoz": SimpleNamespace(fetch=MagicMock(return_value=2))}

    def validate(data):
        if data == 1:
            raise TypeError("bad shape")
        return True

    r = rh.call_with_fallback("depth5", "fetch", validate=validate)
    assert r.source == "meoz"
    assert r.attempts[0] == ("tdxquant", "data")


def test_stale_probe_lease_expires(env):
    _open_primary(env)
    assert rh.pick_source("depth5") == "tdxquant"      # 拿到探测权后从不登记
    assert rh.pick_source("depth5") == "meoz"
    env.clock.advance(rh.PROBE_LEASE_S + 1)
    assert rh.pick_source("depth5") == "tdxquant"


# ---- 问题 3: 单源链不因瞬时熔断断供 (空兜底列表 = 现状)

def test_single_source_chain_keeps_trying_after_transient_failures(env):
    env.chain = ["tdxquant"]
    env.providers = {"tdxquant": SimpleNamespace(get_depth_batch=MagicMock(side_effect=_boom))}
    service = _depth_service()
    for _ in range(5):
        service._call_depth_batch(["A"])
    assert env.providers["tdxquant"].get_depth_batch.call_count == 5


def test_single_source_chain_still_respects_auth_breaker(env):
    env.chain = ["tdxquant"]
    env.providers = {"tdxquant": SimpleNamespace(fetch=MagicMock(side_effect=ProviderAuthError("x")))}
    for _ in range(3):
        rh.call_with_fallback("depth5", "fetch")
    assert env.providers["tdxquant"].fetch.call_count == 1


# ---- 问题 4: 做T 盘口返回 0 只不算源失败

def test_order_books_empty_result_is_not_a_failure(env, monkeypatch):
    env.providers = {"tdxquant": SimpleNamespace(get_depth_batch=MagicMock(return_value={})),
                     "meoz": SimpleNamespace(get_depth_batch=MagicMock(side_effect=_ok))}
    service = _depth_service()
    monkeypatch.setattr(service, "_has_capability", lambda: True)
    for _ in range(4):
        assert service.get_order_books(["SUSPENDED"]) == {}
    env.providers["meoz"].get_depth_batch.assert_not_called()
    assert rh.chain_status("depth5")[0]["state"] == "healthy"


# ---- 问题 5: capset 门控与矩阵 usable 同口径 (兜底链上有插件即授予 depth5)

def test_augment_grants_depth5_from_fallback_chain(monkeypatch):
    from app.data_providers import custom as custom_sources
    from app.services import preferences
    from app.tickflow import policy

    monkeypatch.setattr(preferences, "get_depth5_data_provider", lambda: "tickflow")
    monkeypatch.setattr(preferences, "get_capability_fallbacks",
                        lambda cap_id: ["tdxquant"] if cap_id == "depth5" else [])
    monkeypatch.setattr(custom_sources, "provider_has_dataset",
                        lambda name, ds: (name, ds) == ("tdxquant", "depth5"))
    capset = CapabilitySet({})
    policy._augment_custom_sources(capset)
    assert capset.has(Cap.DEPTH5_BATCH)


# ---- 问题 6: 全链失败不标「兜底」

def test_all_fail_index_quotes_not_marked_degraded(env):
    env.chain = ["yahoo_overseas", "cn_overseas"]
    env.providers = {n: SimpleNamespace(get_overseas_index_quotes=MagicMock(side_effect=TimeoutError))
                     for n in env.chain}
    result = overseas_indices.get_quotes()
    assert result["degraded"] is False
    assert result["reason"]


def test_all_fail_market_game_not_marked_degraded(env):
    env.chain = ["yahoo_overseas", "cn_overseas"]
    env.providers = {n: SimpleNamespace(get_overseas_quotes=MagicMock(side_effect=TimeoutError))
                     for n in env.chain}
    raw = market_game_overseas.collect_overseas_quotes()
    assert raw["degraded"] is False
    ctx = market_game_overseas.build_overseas_context(raw, cutoff=T0)
    assert not any("兜底" in line for line in ctx["limitations"])


# ---- 问题 7: 暂时不可用但已声明的源不从兜底列表中消失

def test_unavailable_declared_plugin_stays_in_fallbacks(monkeypatch):
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    monkeypatch.setattr(custom_sources, "names", lambda: {"meoz"})          # tdxquant check 未通过, 未注册
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, ds: name == "meoz")
    monkeypatch.setattr(custom_sources, "list_plugins", lambda: [
        {"name": "tdxquant", "datasets": ["depth5"], "available": False, "status": "终端未运行"},
        {"name": "meoz", "datasets": ["depth5"], "available": True, "status": "ok"},
    ])
    monkeypatch.setattr(custom_sources, "list_sources", lambda: [])
    monkeypatch.setattr(preferences, "load", lambda: {"depth5_data_provider": "meoz",
                                                      "depth5_data_provider_fallbacks": ["tdxquant"]})
    assert preferences.get_capability_fallbacks("depth5") == ["tdxquant"]
