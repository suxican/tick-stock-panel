"""源中立错误类型与猫爪客户端的映射 (兜底链熔断按这些类型分类)。"""

from __future__ import annotations

import httpx
import pytest

from app.data_providers.errors import (
    ProviderAuthError,
    ProviderDataError,
    ProviderError,
    ProviderQuotaError,
)
from app.plugins.meoz import client as mc


def _client(monkeypatch, handler) -> mc.MeozClient:
    monkeypatch.setattr(mc.time, "sleep", lambda _s: None)
    real = httpx.Client
    monkeypatch.setattr(
        mc.httpx, "Client", lambda **opts: real(transport=httpx.MockTransport(handler), **opts))
    return mc.MeozClient("k-test", min_interval_s=0)


def test_neutral_hierarchy():
    for cls in (ProviderAuthError, ProviderQuotaError, ProviderDataError):
        assert issubclass(cls, ProviderError)
    assert ProviderQuotaError("x").retry_after is None
    assert ProviderQuotaError("x", retry_after=60).retry_after == 60


def test_meoz_auth_error_is_neutral_auth():
    assert issubclass(mc.MeozAuthError, ProviderAuthError)
    with pytest.raises(ProviderAuthError):
        mc.MeozClient("")


def test_meoz_403_raises_neutral_auth(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"code": 403, "message": "denied", "data": None})

    with pytest.raises(ProviderAuthError):
        _client(monkeypatch, handler).query("daily")


def test_meoz_persistent_429_raises_neutral_quota(monkeypatch):
    def handler(request):
        return httpx.Response(429)

    with pytest.raises(ProviderQuotaError) as exc:
        _client(monkeypatch, handler).query("tick")
    # 仍是 MeozError(code=429): 现有调用方 except MeozError 不受影响
    assert isinstance(exc.value, mc.MeozError)
    assert exc.value.code == 429
