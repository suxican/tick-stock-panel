"""龙虎榜能力的注册、主源 getter、兜底默认值与保存接口。"""

from __future__ import annotations

from types import SimpleNamespace

from app.api import settings as settings_api
from app.data_providers import capabilities
from app.data_providers import custom as custom_sources
from app.data_providers.capabilities import CAPABILITY_REGISTRY
from app.services import preferences

_PLUGINS = {"fuyao": {"dragon_tiger", "daily"}, "meoz": {"dragon_tiger", "depth5"}, "tdxquant": {"depth5"}}


def _setup(monkeypatch, stored: dict):
    monkeypatch.setattr(custom_sources, "names", lambda: set(_PLUGINS))
    monkeypatch.setattr(custom_sources, "provider_has_dataset",
                        lambda name, ds: ds in _PLUGINS.get(name, set()))
    monkeypatch.setattr(custom_sources, "list_plugins", lambda: [
        {"name": n, "display_name": n, "datasets": sorted(ds), "available": True, "status": "ok"}
        for n, ds in _PLUGINS.items()])
    monkeypatch.setattr(custom_sources, "list_sources", lambda: [])
    monkeypatch.setattr(preferences, "load", lambda: stored)


def test_registry_has_dragon_tiger_fallback_capability():
    cap = next(c for c in CAPABILITY_REGISTRY if c["id"] == "dragon_tiger")
    assert (cap["field"], cap["default"], cap["tf_tier"], cap.get("fallback")) == \
        ("dragon_tiger_data_provider", "fuyao", None, True)
    assert capabilities.DEFAULT_FALLBACK_ORDER["dragon_tiger"] == ["meoz", "fuyao"]


def test_getter_defaults_and_validates(monkeypatch):
    _setup(monkeypatch, {})
    assert preferences.get_dragon_tiger_data_provider() == "fuyao"
    _setup(monkeypatch, {"dragon_tiger_data_provider": " MEOZ "})
    assert preferences.get_dragon_tiger_data_provider() == "meoz"
    _setup(monkeypatch, {"dragon_tiger_data_provider": "tdxquant"})
    assert preferences.get_dragon_tiger_data_provider() == "fuyao"


def test_default_fallbacks_is_meoz(monkeypatch):
    _setup(monkeypatch, {})
    assert preferences.get_capability_fallbacks("dragon_tiger") == ["meoz"]
    _setup(monkeypatch, {"dragon_tiger_data_provider": "meoz"})
    assert preferences.get_capability_fallbacks("dragon_tiger") == ["fuyao"]


def test_api_saves_dragon_tiger_route_and_fallbacks(monkeypatch):
    saved: dict = {}
    _setup(monkeypatch, saved)
    monkeypatch.setattr(preferences, "save", lambda values: saved.update(values))
    monkeypatch.setattr(settings_api, "detect_capabilities", lambda: "refreshed")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    result = settings_api.update_data_providers(settings_api.DataProvidersIn(
        dragon_tiger_data_provider="meoz", dragon_tiger_data_provider_fallbacks=["fuyao"]), request)
    assert saved == {"dragon_tiger_data_provider": "meoz", "dragon_tiger_data_provider_fallbacks": ["fuyao"]}
    assert result["dragon_tiger_data_provider"] == "meoz"
    assert result["dragon_tiger_data_provider_fallbacks"] == ["fuyao"]
    prefs = settings_api.get_preferences()
    assert prefs["dragon_tiger_data_provider"] == "meoz"
    assert prefs["dragon_tiger_data_provider_fallbacks"] == ["fuyao"]


def test_matrix_row_lists_both_plugins(monkeypatch):
    _setup(monkeypatch, {})
    monkeypatch.setattr("app.tickflow.policy.base_tier_name", lambda: "none")
    row = next(c for c in settings_api.get_capability_matrix()["capabilities"] if c["id"] == "dragon_tiger")
    assert {c["name"] for c in row["candidates"]} == {"fuyao", "meoz"}
    assert row["fallbacks"] == ["meoz"]
    assert row["usable"] is True
