"""兜底顺序偏好: 默认值、清洗 (去重 / 剔除主源 / 未声明源) 与保存接口。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import settings as settings_api
from app.data_providers import capabilities
from app.data_providers import custom as custom_sources
from app.services import preferences

_DECLARED = {
    "tdxquant": {"depth5"},
    "meoz": {"depth5"},
    "fuyao": {"daily"},
    "yahoo_overseas": {"overseas"},
    "cn_overseas": {"overseas"},
}


def _setup(monkeypatch, stored: dict, *, plugins=None):
    plugins = _DECLARED if plugins is None else plugins
    monkeypatch.setattr(custom_sources, "names", lambda: set(plugins))
    monkeypatch.setattr(custom_sources, "provider_has_dataset",
                        lambda name, dataset: dataset in plugins.get(name, set()))
    monkeypatch.setattr(custom_sources, "list_plugins", lambda: _plugin_rows(plugins))
    monkeypatch.setattr(custom_sources, "list_sources", lambda: [])
    monkeypatch.setattr(preferences, "load", lambda: stored)


def _plugin_rows(plugins: dict, pending_remove=()) -> list[dict]:
    return [{"name": n, "datasets": sorted(ds), "available": True, "status": "ok",
             "pending_remove": n in pending_remove} for n, ds in plugins.items()]


def test_registry_marks_only_depth5_and_overseas():
    flagged = {c["id"] for c in capabilities.CAPABILITY_REGISTRY if c.get("fallback")}
    assert flagged == {"depth5", "overseas"}


def test_missing_field_uses_registry_default(monkeypatch):
    _setup(monkeypatch, {"depth5_data_provider": "tdxquant"})
    assert preferences.get_capability_fallbacks("depth5") == ["tickflow", "meoz"]


def test_default_skips_undeclared_sources(monkeypatch):
    _setup(monkeypatch, {"depth5_data_provider": "tickflow"}, plugins={"meoz": {"depth5"}})
    assert preferences.get_capability_fallbacks("depth5") == ["meoz"]


def test_overseas_default_is_the_other_overseas_source(monkeypatch):
    _setup(monkeypatch, {"overseas_data_provider": "yahoo_overseas"})
    assert preferences.get_capability_fallbacks("overseas") == ["cn_overseas"]


def test_fallbacks_dedupe_and_drop_primary(monkeypatch):
    _setup(monkeypatch, {"depth5_data_provider": "tdxquant",
                         "depth5_data_provider_fallbacks": ["meoz", "tdxquant", " MEOZ "]})
    assert preferences.get_capability_fallbacks("depth5") == ["meoz"]


def test_unknown_or_undeclared_source_dropped(monkeypatch):
    _setup(monkeypatch, {"depth5_data_provider": "tdxquant",
                         "depth5_data_provider_fallbacks": ["ghost", "fuyao", "meoz"]})
    assert preferences.get_capability_fallbacks("depth5") == ["meoz"]


def test_tickflow_never_a_fallback_for_overseas(monkeypatch):
    _setup(monkeypatch, {"overseas_data_provider": "yahoo_overseas",
                         "overseas_data_provider_fallbacks": ["tickflow", "cn_overseas"]})
    assert preferences.get_capability_fallbacks("overseas") == ["cn_overseas"]


def test_empty_list_disables(monkeypatch):
    _setup(monkeypatch, {"depth5_data_provider": "tdxquant", "depth5_data_provider_fallbacks": []})
    assert preferences.get_capability_fallbacks("depth5") == []


def test_non_list_value_uses_default(monkeypatch):
    _setup(monkeypatch, {"depth5_data_provider": "tdxquant", "depth5_data_provider_fallbacks": "meoz"})
    assert preferences.get_capability_fallbacks("depth5") == ["tickflow", "meoz"]


def test_non_fallback_capability_returns_empty(monkeypatch):
    _setup(monkeypatch, {})
    assert preferences.get_capability_fallbacks("daily") == []


def _api(monkeypatch, saved: dict, pending_remove=()):
    _setup(monkeypatch, saved)
    monkeypatch.setattr(custom_sources, "list_plugins", lambda: _plugin_rows(_DECLARED, pending_remove))
    monkeypatch.setattr(preferences, "save", lambda values: saved.update(values))
    monkeypatch.setattr(settings_api, "detect_capabilities", lambda: "refreshed")
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))


def test_api_saves_and_returns_fallbacks(monkeypatch):
    saved: dict = {"depth5_data_provider": "tdxquant"}
    request = _api(monkeypatch, saved)
    result = settings_api.update_data_providers(
        settings_api.DataProvidersIn(depth5_data_provider_fallbacks=["meoz"]), request)
    assert saved["depth5_data_provider_fallbacks"] == ["meoz"]
    assert result["depth5_data_provider_fallbacks"] == ["meoz"]
    assert result["overseas_data_provider_fallbacks"] == ["cn_overseas"]
    assert settings_api.get_preferences()["depth5_data_provider_fallbacks"] == ["meoz"]


def test_api_empty_list_is_saved(monkeypatch):
    saved: dict = {"depth5_data_provider": "tdxquant"}
    request = _api(monkeypatch, saved)
    settings_api.update_data_providers(
        settings_api.DataProvidersIn(depth5_data_provider_fallbacks=[]), request)
    assert saved["depth5_data_provider_fallbacks"] == []


def test_api_rejects_pending_remove_in_fallbacks(monkeypatch):
    saved: dict = {"depth5_data_provider": "tdxquant"}
    request = _api(monkeypatch, saved, pending_remove=("meoz",))
    with pytest.raises(HTTPException) as exc:
        settings_api.update_data_providers(
            settings_api.DataProvidersIn(depth5_data_provider_fallbacks=["MEOZ"]), request)
    assert exc.value.status_code == 400
    assert "depth5_data_provider_fallbacks" not in saved
