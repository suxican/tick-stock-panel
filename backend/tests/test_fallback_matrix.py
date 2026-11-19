"""能力矩阵的兜底链字段、门禁标签、重试主源接口与 Key 变更重置。"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import HTTPException

from app.api import settings as settings_api
from app.data_providers import capabilities, routing_health
from app.data_providers import custom as custom_sources
from app.data_providers.errors import ProviderAuthError
from app.market_time import CN_TZ
from app.services import preferences

_PLUGINS = [
    {"name": "tdxquant", "display_name": "TdxQuant", "datasets": ["depth5"], "available": True, "status": "ok"},
    {"name": "meoz", "display_name": "猫爪数据", "datasets": ["depth5"], "available": True, "status": "ok"},
    {"name": "yahoo_overseas", "display_name": "Yahoo", "datasets": ["overseas"], "available": True, "status": "ok"},
]


@pytest.fixture
def env(monkeypatch):
    routing_health.reset()
    plugins = [dict(p) for p in _PLUGINS]
    monkeypatch.setattr(custom_sources, "list_plugins", lambda: plugins)
    monkeypatch.setattr(custom_sources, "list_sources", lambda: [])
    monkeypatch.setattr(routing_health, "cn_now", lambda: datetime(2026, 11, 18, 10, 0, tzinfo=CN_TZ))
    yield plugins
    routing_health.reset()


def _row(matrix: dict, cap_id: str) -> dict:
    return next(c for c in matrix["capabilities"] if c["id"] == cap_id)


def _matrix(primary="tdxquant", fallbacks=("meoz",), tier="none"):
    return capabilities.build_capability_matrix(
        {"depth5_data_provider": primary, "overseas_data_provider": "yahoo_overseas"},
        tickflow_tier=tier,
        fallbacks={"depth5": list(fallbacks), "overseas": []},
    )


def test_healthy_primary_not_degraded(env):
    row = _row(_matrix(), "depth5")
    assert row["fallbacks"] == ["meoz"]
    assert [c["source"] for c in row["chain"]] == ["tdxquant", "meoz"]
    assert (row["active_source"], row["degraded"], row["fallback_reason"]) == ("tdxquant", False, None)


def test_matrix_depth5_exposes_chain_and_degraded(env):
    routing_health.record("depth5", "tdxquant", ProviderAuthError("x"))
    row = _row(_matrix(), "depth5")
    assert row["chain"][0]["state"] == "open"
    assert (row["active_source"], row["degraded"]) == ("meoz", True)
    assert "认证失败" in row["fallback_reason"]
    assert row["effective"] == "tdxquant"          # 主源偏好不变


def test_matrix_non_fallback_capability_has_no_chain_keys(env):
    row = _row(_matrix(), "daily")
    for key in ("fallbacks", "chain", "active_source", "degraded", "fallback_reason"):
        assert key not in row


def test_usable_true_when_primary_unavailable_but_fallback_candidate(env):
    env[0]["available"] = False                     # tdxquant 未就绪
    row = _row(_matrix(), "depth5")
    assert row["usable"] is True
    assert (row["active_source"], row["degraded"]) == ("meoz", True)
    assert "TdxQuant" in row["fallback_reason"]


def test_usable_false_when_chain_empty(env):
    env[0]["available"] = False
    row = _row(_matrix(fallbacks=()), "depth5")
    assert row["usable"] is False
    assert row["degraded"] is False


def test_gate_label_shows_fallback(env, monkeypatch):
    monkeypatch.setattr(preferences, "load", lambda: {"depth5_data_provider": "tdxquant",
                                                      "depth5_data_provider_fallbacks": ["meoz"]})
    monkeypatch.setattr(custom_sources, "names", lambda: {"tdxquant", "meoz", "yahoo_overseas"})
    monkeypatch.setattr(custom_sources, "provider_has_dataset",
                        lambda name, ds: ds in next((p["datasets"] for p in env if p["name"] == name), []))
    routing_health.record("depth5", "tdxquant", ProviderAuthError("x"))
    ok, label = capabilities.capability_gate("depth5", "none")
    assert ok is True
    assert label.startswith("五档盘口 ← 猫爪数据（兜底：")


def test_reset_endpoint_clears_breaker(env, monkeypatch):
    monkeypatch.setattr(settings_api, "get_capability_matrix", lambda: _matrix())
    routing_health.record("depth5", "tdxquant", ProviderAuthError("x"))
    row = settings_api.reset_capability("depth5", settings_api.CapabilityResetIn(source="tdxquant"))
    assert row["id"] == "depth5"
    assert row["degraded"] is False


def test_reset_without_source_clears_whole_capability(env, monkeypatch):
    monkeypatch.setattr(settings_api, "get_capability_matrix", lambda: _matrix())
    routing_health.record("depth5", "tdxquant", ProviderAuthError("x"))
    routing_health.record("depth5", "meoz", ProviderAuthError("x"))
    assert settings_api.reset_capability("depth5", None)["active_source"] == "tdxquant"


@pytest.mark.parametrize("cap_id", ["nope", "daily"])
def test_reset_unknown_or_non_fallback_capability_404(env, cap_id):
    with pytest.raises(HTTPException) as exc:
        settings_api.reset_capability(cap_id, None)
    assert exc.value.status_code == 404


def test_save_plugin_key_resets_breakers(env, monkeypatch):
    routing_health.record("depth5", "meoz", ProviderAuthError("x"))
    routing_health.record("overseas", "meoz", ProviderAuthError("x"))
    monkeypatch.setattr(custom_sources, "probe_plugin_key", lambda name, key: (True, ""))
    monkeypatch.setattr(custom_sources, "refresh_plugin", lambda name, strict=False: None)
    monkeypatch.setattr("app.data_providers.custom.packages.read_state", lambda name: None)
    monkeypatch.setattr(settings_api.secrets_store, "save", lambda values: None)
    monkeypatch.setattr(settings_api.secrets_store, "mask", lambda key: "***")
    settings_api.save_plugin_key(settings_api.PluginKeyIn(plugin="meoz", api_key="k"))
    assert routing_health._health == {}


def test_clear_plugin_key_resets_breakers(env, monkeypatch):
    routing_health.record("depth5", "meoz", ProviderAuthError("x"))
    monkeypatch.setattr(custom_sources, "plugin_manifest", lambda name: {"api_key_env": "MEOZ_API_KEY"})
    monkeypatch.setattr(custom_sources, "refresh_plugin", lambda name, strict=False: None)
    monkeypatch.setattr(settings_api.secrets_store, "clear", lambda key: None)
    settings_api.clear_plugin_key("meoz")
    assert routing_health._health == {}
