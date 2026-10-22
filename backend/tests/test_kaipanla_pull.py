"""Read-only market-emotion supplier contract regressions migrated from can_main."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.data_providers import kaipanla
from app.data_providers.kaipanla_catalog import get_dataset

DAY = date(2026, 9, 30)


@pytest.fixture(autouse=True)
def cache_isolation(monkeypatch):
    kaipanla._CACHE.clear()
    monkeypatch.setattr(kaipanla, "cn_today", lambda: date(2026, 10, 1))
    monkeypatch.setattr(kaipanla, "_MAX_PAGES", 3)


def fake_request(monkeypatch, callback):
    calls = []

    async def request(client, method, url, params):
        calls.append((method, url, dict(params)))
        return callback(params)

    monkeypatch.setattr(kaipanla, "_request", request)
    return calls


async def test_all_five_limit_tiers_are_collected(monkeypatch):
    calls = fake_request(
        monkeypatch, lambda params: {"errcode": "0", "info": [[], DAY.isoformat()]}
    )
    result = await kaipanla.fetch_dataset("ext_kpl_limit_up", DAY)
    assert [call[2]["PidType"] for call in calls] == [1, 2, 3, 4, 5]
    assert result["state"] == "empty"
    calls.clear()
    await kaipanla.fetch_dataset("ext_kpl_limit_up", DAY, {"PidType": 3})
    assert [call[2]["PidType"] for call in calls] == [3]


@pytest.mark.parametrize("aggregate_first", [True, False])
async def test_all_tiers_and_explicit_first_tier_do_not_share_cache(monkeypatch, aggregate_first):
    spec = get_dataset("ext_kpl_limit_up")
    row_width = len([column for column in spec.columns if not column.endswith("_json")])
    fake_request(
        monkeypatch,
        lambda params: {
            "errcode": "0",
            "Total": 1,
            "info": [
                [
                    [f"00000{params['PidType']}", "甲", *([0] * (row_width - 2))],
                ],
                DAY.isoformat(),
            ],
        },
    )
    if aggregate_first:
        all_tiers = await kaipanla.fetch_dataset(spec.id, DAY)
        first_tier = await kaipanla.fetch_dataset(spec.id, DAY, {"PidType": 1})
    else:
        first_tier = await kaipanla.fetch_dataset(spec.id, DAY, {"PidType": 1})
        all_tiers = await kaipanla.fetch_dataset(spec.id, DAY)
    assert len(all_tiers["rows"]) == 5
    assert len(first_tier["rows"]) == 1 and first_tier["rows"][0]["pid_type"] == 1


def test_current_controller_and_indices_preserve_core_contract(monkeypatch):
    monkeypatch.setattr(kaipanla, "cn_today", lambda: DAY)
    spec = get_dataset("ext_kpl_limit_ladder_counts")
    url, params = kaipanla._wire(spec, DAY, {})
    assert "apphwhq" in url and params["c"] == "HomeDingPan"
    url, params = kaipanla._wire(spec, date(2026, 9, 29), {})
    assert "apphis" in url and params["c"] == "HisHomeDingPan"
    spec = get_dataset("ext_kpl_indices")
    _, params = kaipanla._wire(spec, DAY, {})
    assert params["a"] == "RefreshStockList" and params["c"] == "UserSelectStock"
    assert params["StockIDList"] == "SH000001,SZ399001,SZ399006,SH000680"
    assert "Day" not in params


async def test_public_dataset_cache_is_shared_and_immutable(monkeypatch):
    calls = []

    async def request(*args):
        calls.append(1)
        await asyncio.sleep(0.01)
        return {"errcode": "0", "info": {"SJZT": "52", "SJDT": "10"}}

    monkeypatch.setattr(kaipanla, "_request", request)
    a, b = await asyncio.gather(
        *(kaipanla.fetch_dataset("ext_kpl_limit_counts", DAY) for _ in range(2))
    )
    assert len(calls) == 1
    a["rows"][0]["SJZT"] = "999"
    assert b["rows"][0]["SJZT"] == "52"
    assert (await kaipanla.fetch_dataset("ext_kpl_limit_counts", DAY))["rows"][0]["SJZT"] == "52"


async def test_emotion_history_keeps_source_sessions_and_excludes_future(monkeypatch):
    calls = fake_request(
        monkeypatch,
        lambda params: {
            "errcode": "0",
            "info": [
                {"Date": "2026-10-02", "strong": 90, "ztjs": 70},
                {"Date": DAY.isoformat(), "strong": 56, "ztjs": 32},
                {"Date": "2026-09-29", "strong": 60, "ztjs": 48},
            ],
        },
    )
    result = await kaipanla.fetch_dataset("ext_kpl_emotion", DAY, include_history=True)
    assert result["rows"][0]["strong"] == 56
    assert [row["trade_date"] for row in result["history"]] == ["2026-09-29", DAY.isoformat()]
    assert result["history"][0]["date"] == "2026-09-29"
    result["history"][0]["strong"] = 999
    again = await kaipanla.fetch_dataset("ext_kpl_emotion", DAY, include_history=True)
    assert again["history"][0]["strong"] == 60 and len(calls) == 1


async def test_emotion_history_discovers_latest_without_relabelling_holiday(monkeypatch):
    fake_request(
        monkeypatch,
        lambda params: {
            "errcode": "0",
            "info": [
                {"Date": DAY.isoformat(), "strong": 56, "ztjs": 32},
            ],
        },
    )
    result = await kaipanla.fetch_dataset(
        "ext_kpl_emotion", date(2026, 10, 1), include_history=True
    )
    assert result["state"] == "empty" and result["rows"] == []
    assert result["history"][0]["trade_date"] == DAY.isoformat()
    assert result["data_date"] == "2026-10-01"
    with pytest.raises(ValueError, match="未返回请求日期"):
        await kaipanla.fetch_dataset("ext_kpl_emotion", date(2026, 10, 1))


async def test_history_mode_rejects_other_datasets():
    with pytest.raises(ValueError, match="情绪"):
        await kaipanla.fetch_dataset("ext_kpl_limit_counts", DAY, include_history=True)


async def test_broken_limits_numeric_total_uses_offset_and_stops(monkeypatch):
    spec = get_dataset("ext_kpl_broken_limits")
    width = len(spec.columns)
    calls = fake_request(
        monkeypatch,
        lambda params: {
            "errcode": "0",
            "info": [
                [
                    [f"00000{params['Index']}", "甲", *([0] * (width - 2))],
                ],
                2,
            ],
        },
    )
    result = await kaipanla.fetch_dataset(spec.id, DAY, {"PidType": 1})
    assert len(result["rows"]) == 2
    assert [call[2]["Index"] for call in calls] == [0, 1]


@pytest.mark.parametrize("parameters", [{"Token": "x"}, {"UserID": 1}, {"c": "Theme"}])
async def test_credentials_and_controller_override_are_rejected(parameters):
    with pytest.raises(ValueError):
        await kaipanla.fetch_dataset("ext_kpl_limit_counts", DAY, parameters)


def stock_row(index=0):
    width = len(get_dataset("ext_kpl_broken_limits").columns)
    return [f"{index:06}", "甲", *([0] * (width - 2))]


@pytest.mark.parametrize("failure", ["repeated", "incomplete", "max_pages"])
async def test_paginated_stock_list_never_publishes_incomplete_result(monkeypatch, failure):
    def payload(params):
        index = params["Index"]
        rows = (
            []
            if failure == "incomplete" and index
            else [
                stock_row(0 if failure == "repeated" else index),
            ]
        )
        return {"errcode": "0", "info": [rows, 10]}

    fake_request(monkeypatch, payload)
    with pytest.raises(ValueError, match=r"分页未推进|提前返回空页|安全上限"):
        await kaipanla.fetch_dataset("ext_kpl_broken_limits", DAY, {"PidType": 1})
    assert not kaipanla._CACHE


async def test_unknown_total_keeps_fetching_until_complete_empty_page(monkeypatch):
    calls = fake_request(
        monkeypatch,
        lambda params: {
            "errcode": "0",
            "info": [stock_row(params["Index"])] if params["Index"] < 2 else [],
        },
    )
    result = await kaipanla.fetch_dataset("ext_kpl_broken_limits", DAY, {"PidType": 1})
    assert len(result["rows"]) == 2
    assert [call[2]["Index"] for call in calls] == [0, 1, 2]


async def test_failed_forced_refresh_preserves_cached_valid_snapshot(monkeypatch):
    fake_request(monkeypatch, lambda params: {"errcode": "0", "info": {"SJZT": 52, "SJDT": 10}})
    valid = await kaipanla.fetch_dataset("ext_kpl_limit_counts", DAY)
    fake_request(
        monkeypatch,
        lambda params: {
            "errcode": "0",
            "Date": "2026-09-29",
            "info": {"SJZT": 999},
        },
    )
    with pytest.raises(ValueError, match="日期"):
        await kaipanla.fetch_dataset("ext_kpl_limit_counts", DAY, force=True)
    assert await kaipanla.fetch_dataset("ext_kpl_limit_counts", DAY) == valid
