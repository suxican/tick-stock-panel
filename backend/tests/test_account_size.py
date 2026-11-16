"""账户规模: 按生效日期留档, 用于复盘仓位占比的分母。"""
from __future__ import annotations

import json

import pytest

from app.services import preferences


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: path)
    preferences._invalidate_cache()
    yield path
    preferences._invalidate_cache()


def test_add_sorts_and_same_day_overrides():
    assert preferences.get_account_size_history() == []
    preferences.add_account_size("2026-11-20", 600000)
    preferences.add_account_size("2026-11-16", 500000)
    history = preferences.add_account_size("2026-11-20", 650000.5)
    assert history == [{"effective_date": "2026-11-16", "amount": 500000.0},
                       {"effective_date": "2026-11-20", "amount": 650000.5}]


@pytest.mark.parametrize("day,amount", [
    ("2026-11-16", 0), ("2026-11-16", -1), ("2026-11-16", float("nan")), ("2026-11-16", True),
    ("2026-11-16", "500000"), ("2026/11/16", 500000), ("2026-13-01", 500000), (None, 500000),
])
def test_add_rejects_invalid(day, amount):
    with pytest.raises(ValueError):
        preferences.add_account_size(day, amount)
    assert preferences.get_account_size_history() == []


def test_account_size_on_picks_last_effective_not_after_day():
    history = [{"effective_date": "2026-11-16", "amount": 500000.0},
               {"effective_date": "2026-11-20", "amount": 650000.0}]
    assert preferences.account_size_on(history, "2026-11-15") is None
    assert preferences.account_size_on(history, "2026-11-16") == 500000.0
    assert preferences.account_size_on(history, "2026-11-19") == 500000.0
    assert preferences.account_size_on(history, "2026-11-20") == 650000.0
    assert preferences.account_size_on([], "2026-11-20") is None


def test_get_cleans_bad_stored_values(_isolated):
    _isolated.write_text(json.dumps({"account_size_history": [
        {"effective_date": "2026-11-16", "amount": 500000},
        {"effective_date": "bad", "amount": 1},
        {"effective_date": "2026-11-17", "amount": -5},
        "junk",
    ]}), encoding="utf-8")
    preferences._invalidate_cache()
    assert preferences.get_account_size_history() == [{"effective_date": "2026-11-16", "amount": 500000.0}]
    _isolated.write_text(json.dumps({"account_size_history": {"x": 1}}), encoding="utf-8")
    preferences._invalidate_cache()
    assert preferences.get_account_size_history() == []


def test_api_put_get_and_invalid():
    from fastapi import HTTPException

    from app.api.settings import AccountSizeIn, get_account_size, update_account_size

    assert update_account_size(AccountSizeIn(effective_date="2026-11-16", amount=500000)) == {
        "history": [{"effective_date": "2026-11-16", "amount": 500000.0}]}
    assert get_account_size()["history"][0]["amount"] == 500000.0
    with pytest.raises(HTTPException) as exc:
        update_account_size(AccountSizeIn(effective_date="2026-11-16", amount=-1))
    assert exc.value.status_code == 400
