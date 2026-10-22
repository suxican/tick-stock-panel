"""Minute observations keep repository units and frozen-plan constraints."""
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.market_game_capital import build_capital_behavior
from app.services.market_game_capital_service import _links
from app.services.market_game_disclosures import fetch_institutional_disclosures

DAY = date(2026, 10, 9)
NOW = datetime(2026, 10, 9, 10, 31, tzinfo=CN_TZ)


def _minute_repository(monkeypatch, *, historical_days=60):
    days = [DAY - timedelta(days=index) for index in range(historical_days, -1, -1)]
    frame = pl.DataFrame([{
        "symbol": "600000.SH", "datetime": datetime.combine(day, time(9, 30)) + timedelta(minutes=index),
        "open": 10., "high": 10.1, "low": 9.9, "close": 10.,
        "volume": 100., "amount": 100_000.,
    } for day in days for index in range(61)])

    class Repo:
        def list_minute_dates(self, start, end, *, asset_type):
            assert asset_type == "stock"
            assert start < end == DAY
            return days

        def get_minute_by_dates(self, symbols, requested, *, asset_type):
            assert symbols == ["600000.SH"] and asset_type == "stock"
            assert requested == days
            return self.frame

    repo = Repo()
    repo.frame = frame
    monkeypatch.setattr("app.services.market_game_capital._observation_date", lambda *_: (DAY, True))
    monkeypatch.setattr("app.services.market_game_capital.capital_background", lambda *_, **__: (True, "verified"))
    return repo


def _plan():
    return {"cutoff": "2026-10-08T16:00:00+08:00", "candidates": [{
        "symbol": "600000.SH", "name": "浦发银行", "sector": "银行",
        "max_position": .1, "conditions": [{"description": "价格和市场条件仍需核验"}],
    }], "validity": {
        "status": "scheduled", "entry_session": "2026-10-09",
        "expires_at": "2026-10-13T15:00:00+08:00",
    }}


def test_local_minute_observation_uses_lots_yuan_and_same_clock_history(monkeypatch):
    repo = _minute_repository(monkeypatch)
    value = build_capital_behavior(repo, _plan(), {}, now=NOW)
    assert value["status"] == "ready"
    row = value["rows"][0]
    assert row["reference_price"] == 10.
    assert row["participation"] == {"score": 50., "raw_value": 1., "sample_size": 60}
    assert row["windows"][-1]["return"] == 0.
    assert row["as_of"] == "2026-10-09T10:30:00+08:00"


def test_incomplete_history_retains_known_prices_without_invented_score(monkeypatch):
    repo = _minute_repository(monkeypatch, historical_days=59)
    value = build_capital_behavior(repo, _plan(), {}, now=NOW)
    assert value["status"] == "limited"
    assert value["rows"][0]["reference_price"] == 10.
    assert value["rows"][0]["participation"]["score"] is None
    assert value["rows"][0]["scenario"]["id"] == "unconfirmed"


@pytest.mark.parametrize("defect", ["timezone", "unit", "gap"])
def test_minute_contract_defects_fail_closed(monkeypatch, defect):
    repo = _minute_repository(monkeypatch)
    if defect == "timezone":
        repo.frame = repo.frame.with_columns(pl.col("datetime").dt.replace_time_zone("UTC"))
    elif defect == "unit":
        repo.frame = repo.frame.with_columns((pl.col("volume") * 100).alias("volume"))
    else:
        repo.frame = repo.frame.filter(pl.col("datetime") != datetime(2026, 10, 9, 10))
    value = build_capital_behavior(repo, _plan(), {}, now=NOW)
    assert value["status"] == "unavailable"
    assert value["rows"][0]["reference_price"] is None
    assert value["rows"][0]["participation"]["score"] is None


def test_missing_minute_capability_preserves_report_and_returns_unavailable(monkeypatch):
    monkeypatch.setattr("app.services.market_game_capital._observation_date", lambda *_: (DAY, True))
    monkeypatch.setattr("app.services.market_game_capital.capital_background", lambda *_, **__: (False, "missing"))
    report = _plan()
    frozen = deepcopy(report)
    value = build_capital_behavior(SimpleNamespace(), report, {}, now=NOW)
    assert value["status"] == "unavailable"
    assert value["rows"][0]["scenario"]["id"] == "unconfirmed"
    assert report == frozen


def test_negative_observation_remains_binding_after_recovery():
    report = _plan()
    behavior = {"data_date": DAY.isoformat(), "rows": [{
        "symbol": "600000.SH", "status": "ready", "scenario": {"id": "panic_withdrawal", "label": "撤退"},
    }]}
    blocked = _links(report, behavior, [], NOW, True)
    assert blocked[0]["status"] == "blocked" and blocked[0]["retained_cap"] == 0
    behavior["rows"][0]["scenario"]["id"] = "trend_feedback"
    history = [{"created_at": NOW.isoformat(), "plan_links": blocked}]
    assert _links(report, behavior, history, NOW, True)[0]["status"] == "blocked"


@pytest.mark.parametrize("source_state", ["source_unavailable", "no_data"])
def test_optional_disclosure_source_states_are_explicit(tmp_path, monkeypatch, source_state):
    monkeypatch.setattr("app.services.market_game_disclosures.dragon_tiger.get_dragon_tiger",
                        lambda *_: {"state": source_state})
    result = fetch_institutional_disclosures(tmp_path, now=NOW, target=date(2026, 10, 8))
    assert result["state"] == source_state
    assert result["rows"] == [] and result["available_at"] is None


def test_disclosures_keep_subboard_date_zero_and_unknown_rate_unit(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.market_game_disclosures.dragon_tiger.get_dragon_tiger", lambda *_: {
        "state": "ok", "trade_date": "2026-10-09", "all": {"trade_date": "2026-10-09"},
        "org": {"trade_date": "2026-10-08", "stock_items": [{
            "thscode": "600000.SH", "range_days": 1,
            "org_net_value": 0, "org_net_rate": .3, "org_buy_num": 0,
        }]},
    })
    result = fetch_institutional_disclosures(tmp_path, now=NOW, target=DAY)
    assert result["state"] == "fallback_prev"
    assert result["trade_date"] == "2026-10-08"
    assert result["rows"][0]["org_net_value"] == 0
    assert result["rows"][0]["org_net_rate"] is None
    assert result["point_in_time_verified"] is False
