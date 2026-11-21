"""精选板块正宗度: 按声明数据集的数据源取数, 作为持仓分析证据; 不可用不当作「不正宗」。"""
from types import SimpleNamespace
from unittest.mock import Mock

from app.services import theme_authenticity as ta

RAW = {"688388.SH": [{"theme_id": "801004k", "theme_name": "锂电池", "change_pct": -0.03452}],
       "600183.SH": []}


def _sources(monkeypatch, provider, declared=("plugin_x",)):
    monkeypatch.setattr(ta.custom_sources, "names", lambda: {"plugin_x", "other"})
    monkeypatch.setattr(ta.custom_sources, "provider_has_dataset", lambda name, ds: name in declared)
    monkeypatch.setattr(ta.custom_sources, "get_provider", lambda name: provider)
    ta._cache.clear()


def test_fetch_uses_declaring_source_and_caches(monkeypatch):
    provider = SimpleNamespace(get_theme_authenticity=Mock(return_value=RAW))
    _sources(monkeypatch, provider)
    assert ta.fetch_authenticity(["688388.SH", "600183.SH"]) == RAW
    assert ta.fetch_authenticity(["600183.SH", "688388.SH"]) == RAW
    provider.get_theme_authenticity.assert_called_once_with(["600183.SH", "688388.SH"])


def test_fetch_unavailable_without_source_or_on_error(monkeypatch):
    _sources(monkeypatch, SimpleNamespace(), declared=())
    assert ta.fetch_authenticity(["688388.SH"]) is None
    _sources(monkeypatch, SimpleNamespace(get_theme_authenticity=Mock(side_effect=RuntimeError("down"))))
    assert ta.fetch_authenticity(["688388.SH"]) is None


def test_summary_and_evidence_texts():
    hit = ta.summarize(RAW, "688388.SH")
    assert hit == {"available": True, "themes": RAW["688388.SH"]}
    assert ta.authenticity_evidence(hit) == "精选板块正宗度（数据源当前名单）：锂电池（最正宗，当日 -3.45%）"
    miss = ta.summarize(RAW, "600183.SH")
    assert ta.authenticity_evidence(miss) == "精选板块正宗度（数据源当前名单）：未列入任何精选板块的最正宗名单"
    nameless = ta.summarize({"x": [{"theme_id": "801660k", "theme_name": None, "change_pct": None}]}, "x")
    assert ta.authenticity_evidence(nameless) == "精选板块正宗度（数据源当前名单）：801660k（最正宗，当日涨跌未知）"
    unknown = ta.summarize(None, "688388.SH")
    assert unknown == {"available": False}
    assert ta.authenticity_evidence(unknown) == "精选板块正宗度：来源不可用，不能视为不正宗"


def test_fetch_leaders_caches_and_marks_unavailable(monkeypatch):
    ta._leaders_cache.clear()
    rows = [{"theme_id": "801004k", "theme_name": "锂电池", "change_pct": -0.03, "symbols": ["688388"]}]
    provider = SimpleNamespace(get_theme_leaders=Mock(return_value=rows))
    _sources(monkeypatch, provider)
    assert ta.fetch_leaders() == rows
    assert ta.fetch_leaders() == rows
    provider.get_theme_leaders.assert_called_once_with()
    ta._leaders_cache.clear()
    _sources(monkeypatch, SimpleNamespace(get_theme_leaders=Mock(side_effect=RuntimeError("down"))))
    assert ta.fetch_leaders() is None
