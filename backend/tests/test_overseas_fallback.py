"""外围行情兜底链: 单次请求结果只来自一个源, 结果标注实际来源与降级原因。"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.data_providers import routing_health as rh
from app.services import market_game_overseas, overseas_indices

_DAY = "2026-11-17"
_QUOTE = {"symbol": "^N225", "state": "ready", "session_date": _DAY,
          "price": 101.5, "previous_close": 100.0, "change_pct": 0.015}
_BAR = {"symbol": "^N225", "date": _DAY, "open": 100, "low": 99, "high": 102, "close": 101.5}


def _provider(**methods):
    return SimpleNamespace(**{name: MagicMock(**spec) for name, spec in methods.items()})


@pytest.fixture
def env(monkeypatch):
    rh.reset()
    overseas_indices._work.clear()
    state = SimpleNamespace(
        chain=["yahoo_overseas", "cn_overseas"],
        providers={
            "yahoo_overseas": _provider(get_overseas_index_quotes={"return_value": [_QUOTE]},
                                        get_overseas_index_daily={"return_value": [_BAR]},
                                        get_overseas_quotes={"return_value": {"fetched_at": "t", "quotes": [1]}}),
            "cn_overseas": _provider(get_overseas_index_quotes={"return_value": [_QUOTE]},
                                     get_overseas_index_daily={"return_value": [_BAR]},
                                     get_overseas_quotes={"return_value": {"fetched_at": "t2", "quotes": [2]}}),
        },
    )
    monkeypatch.setattr(rh, "_primary", lambda cap_id: state.chain[0])
    monkeypatch.setattr(rh, "_fallbacks", lambda cap_id: state.chain[1:])
    monkeypatch.setattr(rh, "_candidate_names", lambda cap_id: set(state.providers))
    monkeypatch.setattr(rh, "resolve_provider", lambda name: state.providers[name])
    monkeypatch.setattr(rh, "_display", lambda name: name)
    yield state
    rh.reset()


def test_index_quotes_primary_not_degraded(env):
    result = overseas_indices.get_quotes()
    assert (result["source"], result["degraded"], result["fallback_reason"]) == ("yahoo_overseas", False, None)
    assert [r["symbol"] for r in result["rows"]] == ["^N225"]


def test_index_quotes_fall_back_on_exception(env):
    env.providers["yahoo_overseas"].get_overseas_index_quotes.side_effect = TimeoutError
    result = overseas_indices.get_quotes()
    assert (result["source"], result["degraded"]) == ("cn_overseas", True)
    assert "yahoo_overseas" in result["fallback_reason"]
    assert result["rows"]


def test_daily_empty_list_does_not_switch(env):
    env.providers["yahoo_overseas"].get_overseas_index_daily.return_value = []
    result = overseas_indices.get_daily("^N225", date(2026, 11, 17), date(2026, 11, 17))
    assert (result["source"], result["degraded"], result["rows"]) == ("yahoo_overseas", False, [])
    env.providers["cn_overseas"].get_overseas_index_daily.assert_not_called()


def test_daily_one_series_one_source(env):
    env.providers["yahoo_overseas"].get_overseas_index_daily.side_effect = RuntimeError
    result = overseas_indices.get_daily("^N225", date(2026, 11, 17), date(2026, 11, 17))
    assert result["source"] == "cn_overseas"
    assert len(result["rows"]) == 1


def test_all_sources_fail_keeps_reason(env):
    for p in env.providers.values():
        p.get_overseas_index_quotes.side_effect = TimeoutError
    result = overseas_indices.get_quotes()
    assert result["rows"] == []
    assert result["reason"]
    assert result["degraded"] is True


def test_cache_key_separates_sources(env, monkeypatch):
    keys = []

    def run(key, fn, ttl_s):
        keys.append(key[0])
        return fn()

    monkeypatch.setattr(overseas_indices._work, "run", run)
    env.providers["yahoo_overseas"].get_overseas_index_quotes.side_effect = TimeoutError
    overseas_indices.get_quotes()
    assert keys == ["yahoo_overseas", "cn_overseas"]


def test_market_game_quotes_primary(env):
    result = market_game_overseas.collect_overseas_quotes()
    assert (result["source"], result["quotes"], result["degraded"]) == ("yahoo_overseas", [1], False)


def test_market_game_quotes_fall_back_and_label_source(env):
    env.providers["yahoo_overseas"].get_overseas_quotes.side_effect = TimeoutError
    result = market_game_overseas.collect_overseas_quotes()
    assert (result["source"], result["fetched_at"], result["quotes"]) == ("cn_overseas", "t2", [2])
    assert result["degraded"] is True
    assert result["fallback_reason"]


def test_market_game_invalid_shape_counts_failure_and_falls_back(env):
    env.providers["yahoo_overseas"].get_overseas_quotes.return_value = {"quotes": None}
    result = market_game_overseas.collect_overseas_quotes()
    assert result["source"] == "cn_overseas"


def test_market_game_all_fail_returns_empty_with_reason(env):
    for p in env.providers.values():
        p.get_overseas_quotes.side_effect = TimeoutError
    result = market_game_overseas.collect_overseas_quotes()
    assert (result["quotes"], result["fetched_at"]) == ([], None)
    assert result["reason"]


def test_overseas_context_carries_degraded_flag():
    from datetime import datetime

    from app.market_time import CN_TZ

    cutoff = datetime(2026, 11, 18, 10, 0, tzinfo=CN_TZ)
    raw = {"source": "cn_overseas", "fetched_at": cutoff.isoformat(), "quotes": [],
           "degraded": True, "fallback_reason": "yahoo_overseas：连续 3 次失败"}
    ctx = market_game_overseas.build_overseas_context(raw, cutoff=cutoff)
    assert (ctx["source"], ctx["degraded"], ctx["fallback_reason"]) == ("cn_overseas", True, raw["fallback_reason"])
    assert any("兜底" in line for line in ctx["limitations"])
    plain = market_game_overseas.build_overseas_context({**raw, "degraded": False, "fallback_reason": None},
                                                        cutoff=cutoff)
    assert plain["degraded"] is False
    assert not any("兜底" in line for line in plain["limitations"])


def test_stored_context_without_new_fields_still_validates():
    from app.services.market_game_overseas_models import OverseasContext

    old = {"source": "yahoo_overseas", "fetched_at": None, "cutoff": "2026-11-18T10:00:00+08:00",
           "state": "unavailable", "quotes": [], "factors": [], "summary": "s", "psychology_link": "p",
           "risk_state": "unknown", "affected_sectors": [], "limitations": []}
    assert OverseasContext.model_validate(old).degraded is False


def test_market_game_quotes_are_copied(env):
    result = market_game_overseas.collect_overseas_quotes()
    result["quotes"].append(99)
    assert env.providers["yahoo_overseas"].get_overseas_quotes.return_value["quotes"] == [1]
