"""Overseas routing stays independent of domestic providers and subscription tiers."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import settings as settings_api
from app.data_providers import capabilities
from app.data_providers import custom as custom_sources
from app.data_providers.custom.config import CustomSourceConfig, DatasetConfig, config_from_dict
from app.data_providers.custom.provider import GenericHTTPProvider
from app.services import preferences


def _sources(monkeypatch, *, available=True):
    monkeypatch.setattr(custom_sources, "list_sources", lambda: [])
    monkeypatch.setattr(custom_sources, "list_plugins", lambda: [{
        "name": "yahoo_overseas", "display_name": "Yahoo", "datasets": ["overseas"],
        "available": available, "status": "ok" if available else "unavailable",
    }])
    monkeypatch.setattr(custom_sources, "names", lambda: ["yahoo_overseas", "domestic", "alternate"])
    monkeypatch.setattr(custom_sources, "provider_has_dataset",
                        lambda name, dataset: name in {"yahoo_overseas", "alternate"} and dataset == "overseas")
    monkeypatch.setattr(preferences, "load", lambda: {})


@pytest.mark.parametrize("tier", ["none", "expert"])
def test_overseas_matrix_and_runtime_gate_use_plugin_not_tickflow(monkeypatch, tier):
    _sources(monkeypatch)
    matrix = capabilities.build_capability_matrix({}, tickflow_tier=tier)
    overseas = next(cap for cap in matrix["capabilities"] if cap["id"] == "overseas")
    assert overseas["effective"] == "yahoo_overseas"
    assert overseas["tf_tier"] is None
    assert overseas["tf_available"] is False
    assert overseas["usable"] is True
    assert [source["name"] for source in overseas["candidates"]] == ["yahoo_overseas"]
    assert capabilities.capability_gate("overseas", tier) == (True, "外围行情 ← Yahoo")


def test_unavailable_overseas_plugin_does_not_fall_back_to_domestic(monkeypatch):
    _sources(monkeypatch, available=False)
    overseas = next(cap for cap in capabilities.build_capability_matrix({}, "expert")["capabilities"]
                    if cap["id"] == "overseas")
    assert overseas["candidates"] == []
    assert overseas["usable"] is False
    assert overseas["pending"][0]["name"] == "yahoo_overseas"
    assert capabilities.capability_gate("overseas", "expert")[0] is False


@pytest.mark.parametrize("stored,expected", [
    (None, "yahoo_overseas"), ("ghost", "yahoo_overseas"),
    ("tickflow", "yahoo_overseas"), ("domestic", "yahoo_overseas"),
    (" ALTERNATE ", "alternate"),
])
def test_preference_validates_dataset_and_defaults_old_configs(monkeypatch, stored, expected):
    _sources(monkeypatch)
    monkeypatch.setattr(preferences, "load", lambda: {"overseas_data_provider": stored})
    assert preferences.get_overseas_data_provider() == expected


def test_api_preserves_independent_overseas_route(monkeypatch):
    _sources(monkeypatch)
    saved = {}
    monkeypatch.setattr(preferences, "save", lambda values: saved.update(values))
    monkeypatch.setattr(preferences, "load", lambda: saved)
    monkeypatch.setattr(settings_api, "detect_capabilities", lambda: "refreshed")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    result = settings_api.update_data_providers(
        settings_api.DataProvidersIn(overseas_data_provider="alternate"), request,
    )
    assert saved == {"overseas_data_provider": "alternate"}
    assert result["overseas_data_provider"] == "alternate"
    assert result["daily_data_provider"] == "tickflow"


def test_generic_yaml_rejects_overseas_instead_of_silently_losing_dataset():
    with pytest.raises(ValueError, match="Python 插件"):
        config_from_dict({"name": "test", "datasets": {"overseas": {"url": "https://example.test"}}})
    provider = GenericHTTPProvider(CustomSourceConfig(
        name="test", display_name="Test", datasets={"overseas": DatasetConfig(url="https://example.test")},
    ))
    try:
        assert any("Python 插件" in error for error in provider.validate())
    finally:
        provider.close()


def test_create_provider_rejects_overseas_before_sanitizing():
    with pytest.raises(ValueError, match="Python 插件"):
        provider = custom_sources.create_provider({
            "name": "overseas_probe", "datasets": {"overseas": {"url": "https://example.test"}},
        })
        provider.close()


def test_settings_save_rejects_overseas_yaml_without_writing(monkeypatch):
    writes = []
    monkeypatch.setattr(custom_sources, "save_config", lambda *_args: writes.append(True))
    monkeypatch.setattr(custom_sources, "load_all", lambda: None)
    monkeypatch.setattr(settings_api, "list_data_sources", lambda: {})
    app = FastAPI()
    app.include_router(settings_api.router)
    with TestClient(app) as client:
        response = client.post("/api/settings/data-sources", json={
            "name": "overseas_probe", "datasets": {"overseas": {"url": "https://example.test"}},
        })
    assert response.status_code == 400
    assert "Python 插件" in response.json()["detail"]
    assert writes == []


@pytest.mark.parametrize("operation", ["uninstall", "delete"])
def test_removed_overseas_source_clears_stored_route_even_after_getter_falls_back(monkeypatch, operation):
    _sources(monkeypatch)
    saved = {"overseas_data_provider": "alternate"}
    monkeypatch.setattr(preferences, "load", lambda: dict(saved))
    monkeypatch.setattr(preferences, "save", lambda values: saved.update(values))
    monkeypatch.setattr(custom_sources, "names", lambda: ["yahoo_overseas"])
    monkeypatch.setattr(custom_sources, "load_all", lambda: None)
    monkeypatch.setattr(settings_api, "list_data_sources", lambda: {})
    monkeypatch.setattr(settings_api, "detect_capabilities", lambda: "refreshed")
    assert preferences.get_overseas_data_provider() == "yahoo_overseas"
    if operation == "uninstall":
        monkeypatch.setattr(custom_sources, "is_builtin", lambda _name: True)
        monkeypatch.setattr(custom_sources, "uninstall_plugin", lambda _name: (True, "removed"))
        settings_api.uninstall_plugin("alternate")
    else:
        monkeypatch.setattr(custom_sources, "delete_config", lambda _name: None)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        settings_api.delete_data_source("alternate", request)
    assert saved == {"overseas_data_provider": "yahoo_overseas"}
