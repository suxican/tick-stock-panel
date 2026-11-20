"""内置插件的 provider.config.datasets 须与 plugin.yaml 声明一致。

provider_has_dataset 读的是 provider.config.datasets; 漏声明会让偏好 getter
把用户选的源静默回退成默认源 (如龙虎榜无法切到猫爪)。
"""
from pathlib import Path

import pytest
import yaml

from app.plugins.fuyao.provider import _FuyaoConfig
from app.plugins.meoz.provider import _MeozConfig

_PLUGINS = Path(__file__).resolve().parents[1] / "app" / "plugins"


@pytest.mark.parametrize("name,config_cls", [("meoz", _MeozConfig), ("fuyao", _FuyaoConfig)])
def test_provider_config_datasets_match_manifest(name, config_cls):
    manifest = yaml.safe_load((_PLUGINS / name / "plugin.yaml").read_text(encoding="utf-8"))
    assert set(config_cls().datasets) == set(manifest["datasets"])
