"""兜底链调度与熔断 (routing_health): 用假 provider 与可控北京时间, 不触网。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.data_providers import routing_health as rh
from app.data_providers.errors import ProviderAuthError, ProviderDataError, ProviderQuotaError
from app.market_time import CN_TZ

T0 = datetime(2026, 11, 18, 10, 0, 0, tzinfo=CN_TZ)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class _Fake:
    """可编程 provider: behaviour 为返回值或异常实例 (每次调用取下一个, 用尽后重复最后一个)。"""

    def __init__(self, *behaviour) -> None:
        self.behaviour = list(behaviour) or [{"ok": True}]
        self.calls = 0

    def fetch(self, *args, **kwargs):
        self.calls += 1
        item = self.behaviour[min(self.calls - 1, len(self.behaviour) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def env(monkeypatch):
    rh.reset()
    clock = _Clock(T0)
    providers = {"tdxquant": _Fake(), "tickflow": _Fake(), "meoz": _Fake()}
    state = SimpleNamespace(primary="tdxquant", fallbacks=["tickflow", "meoz"],
                            candidates={"tdxquant", "tickflow", "meoz"}, providers=providers, clock=clock)
    monkeypatch.setattr(rh, "cn_now", clock)
    monkeypatch.setattr(rh, "_primary", lambda cap_id: state.primary)
    monkeypatch.setattr(rh, "_fallbacks", lambda cap_id: list(state.fallbacks))
    monkeypatch.setattr(rh, "_candidate_names", lambda cap_id: set(state.candidates))
    monkeypatch.setattr(rh, "_display", lambda name: name)

    def resolve(name):
        if name not in state.providers:
            raise LookupError(name)
        return state.providers[name]

    monkeypatch.setattr(rh, "resolve_provider", resolve)
    yield state
    rh.reset()


def _call(**kw):
    return rh.call_with_fallback("depth5", "fetch", ["A"], **kw)


def test_primary_ok_not_degraded(env):
    r = _call()
    assert (r.source, r.degraded, r.reason) == ("tdxquant", False, None)
    assert r.data == {"ok": True}
    assert r.attempts == (("tdxquant", "ok"),)


def test_auth_error_switches_and_opens_until_reset(env):
    env.providers["tdxquant"] = _Fake(ProviderAuthError("bad key"))
    r = _call()
    assert (r.source, r.degraded) == ("tickflow", True)
    assert "认证失败" in r.reason
    env.clock.advance(86400 * 3)
    _call()
    assert env.providers["tdxquant"].calls == 1          # 熔断中不再调用
    rh.reset("depth5", "tdxquant")
    _call()
    assert env.providers["tdxquant"].calls == 2


def test_three_transient_failures_open_300s_then_half_open(env):
    env.providers["tdxquant"] = _Fake(TimeoutError(), TimeoutError(), TimeoutError(), {"ok": 1})
    for _ in range(3):
        assert _call().source == "tickflow"
    assert env.providers["tdxquant"].calls == 3
    state = rh.chain_status("depth5")[0]
    assert state["state"] == "open"
    assert "连续 3 次失败" in state["reason"]
    env.clock.advance(299)
    _call()
    assert env.providers["tdxquant"].calls == 3
    env.clock.advance(2)
    assert rh.chain_status("depth5")[0]["state"] == "half_open"
    r = _call()
    assert (r.source, r.degraded) == ("tdxquant", False)
    assert rh.chain_status("depth5")[0]["state"] == "healthy"


def test_half_open_probe_failure_reopens_300s(env):
    env.providers["tdxquant"] = _Fake(TimeoutError())
    for _ in range(3):
        _call()
    env.clock.advance(301)
    _call()                                   # 探测失败
    assert env.providers["tdxquant"].calls == 4
    status = rh.chain_status("depth5")[0]
    assert status["state"] == "open"
    assert status["until"] == (env.clock.now + timedelta(seconds=300)).isoformat()


def test_success_resets_consecutive_count(env):
    env.providers["tdxquant"] = _Fake(TimeoutError(), TimeoutError(), {"ok": 1}, TimeoutError(), TimeoutError())
    for _ in range(5):
        _call()
    assert rh.chain_status("depth5")[0]["state"] == "healthy"


def test_quota_open_until_beijing_midnight(env):
    env.clock.now = datetime(2026, 11, 18, 23, 59, 0, tzinfo=CN_TZ)
    env.providers["tdxquant"] = _Fake(ProviderQuotaError("429"), {"ok": 1})
    _call()
    status = rh.chain_status("depth5")[0]
    assert status["until"] == datetime(2026, 11, 19, 0, 0, tzinfo=CN_TZ).isoformat()
    env.clock.advance(61)
    assert rh.chain_status("depth5")[0]["state"] == "half_open"


def test_quota_retry_after_shorter_wins(env):
    env.providers["tdxquant"] = _Fake(ProviderQuotaError("429", retry_after=60))
    _call()
    assert rh.chain_status("depth5")[0]["until"] == (T0 + timedelta(seconds=60)).isoformat()


def test_validate_false_counts_as_data_failure(env):
    r = _call(validate=lambda data: False)
    assert r.source is None
    assert {a[1] for a in r.attempts} == {"data"}


def test_data_error_counts_as_failure(env):
    env.providers["tdxquant"] = _Fake(ProviderDataError("empty"))
    assert _call().attempts[0] == ("tdxquant", "data")


def test_all_open_returns_without_calling(env):
    for name in env.providers:
        env.providers[name] = _Fake(ProviderAuthError("x"))
    _call()
    calls = {n: p.calls for n, p in env.providers.items()}
    r = _call()
    assert (r.data, r.source, r.degraded) == (None, None, True)
    assert {n: p.calls for n, p in env.providers.items()} == calls
    assert all(outcome == "open" for _, outcome in r.attempts)
    assert r.reason


def test_half_open_admits_single_probe(env):
    env.providers["tdxquant"] = _Fake(TimeoutError())
    for _ in range(3):
        _call()
    env.clock.advance(301)
    picked: list[str | None] = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        picked.append(rh.pick_source("depth5"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(picked) == ["tdxquant", "tickflow"]


def test_pick_source_exclude(env):
    assert rh.pick_source("depth5", exclude={"tdxquant"}) == "tickflow"
    assert rh.pick_source("depth5", exclude={"tdxquant", "tickflow", "meoz"}) is None


def test_release_frees_half_open_probe(env):
    env.providers["tdxquant"] = _Fake(TimeoutError())
    for _ in range(3):
        _call()
    env.clock.advance(301)
    assert rh.pick_source("depth5") == "tdxquant"
    assert rh.pick_source("depth5") == "tickflow"
    rh.release("depth5", "tdxquant")
    assert rh.pick_source("depth5") == "tdxquant"


def test_chain_skips_source_no_longer_candidate(env):
    env.candidates = {"tdxquant", "tickflow"}
    assert rh.effective_chain("depth5") == ["tdxquant", "tickflow"]


def test_chain_dedupes_primary(env):
    env.fallbacks = ["tdxquant", "meoz", "meoz"]
    assert rh.effective_chain("depth5") == ["tdxquant", "meoz"]


def test_primary_not_candidate_is_degraded(env):
    env.candidates = {"tickflow", "meoz"}
    r = _call()
    assert (r.source, r.degraded) == ("tickflow", True)
    assert "tdxquant" in r.reason


def test_unresolved_provider_does_not_open_breaker(env):
    del env.providers["tdxquant"]
    for _ in range(5):
        r = _call()
    assert r.attempts[0] == ("tdxquant", "unresolved")
    assert rh.chain_status("depth5")[0]["state"] == "healthy"


def test_empty_fallbacks_equals_current_behavior(env):
    env.fallbacks = []
    env.providers["tdxquant"] = _Fake(TimeoutError())
    r = _call()
    assert (r.data, r.source) == (None, None)
    assert r.attempts == (("tdxquant", "error"),)
    assert env.providers["meoz"].calls == 0


def test_invoke_wrapper_receives_source(env):
    env.providers["tdxquant"] = _Fake(TimeoutError())
    seen: list[str] = []

    def invoke(src, fn):
        seen.append(src)
        return fn()

    _call(invoke=invoke)
    assert seen == ["tdxquant", "tickflow"]


def test_reason_never_contains_key(env):
    env.providers["tdxquant"] = _Fake(ProviderAuthError("apikey=SECRET-123 rejected"))
    r = _call()
    assert "SECRET" not in (r.reason or "")
    assert all("SECRET" not in (s["reason"] or "") for s in rh.chain_status("depth5"))


def test_reset_all_sources_of_one_plugin(env):
    env.providers["tdxquant"] = _Fake(ProviderAuthError("x"), {"ok": 1})
    _call()
    rh.reset(source="tdxquant")
    assert rh.chain_status("depth5")[0]["state"] == "healthy"
