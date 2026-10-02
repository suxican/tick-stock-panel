"""Anonymous request, pagination, cache, and complete-snapshot publication contracts."""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.kaipanla import router
from app.services import ext_pull, kaipanla
from app.services.ext_data import ExtConfig, ExtField, rows_to_parquet
from app.services.kaipanla_catalog import get_dataset, presets

DAY = date(2026, 9, 30)


@pytest.fixture(autouse=True)
def cache_isolation(monkeypatch):
    kaipanla._CACHE.clear()
    monkeypatch.setattr(kaipanla, "cn_today", lambda: date(2026, 10, 1))
    monkeypatch.setattr(kaipanla, "_MAX_PAGES", 3)


def preset(name):
    return next(config for config in presets() if config.id == f"ext_kpl_{name}")


def fake_request(monkeypatch, callback):
    calls = []

    async def request(client, method, url, params):
        calls.append((method, url, dict(params)))
        return callback(params)

    monkeypatch.setattr(kaipanla, "_request", request)
    return calls


async def test_offset_pagination_uses_raw_rows_and_form_post(monkeypatch):
    calls = fake_request(monkeypatch, lambda params: {
        "errcode": "0", "Time": DAY.isoformat(), "Total": 3,
        "list": ([{"ID": "000001", "Name": "甲", "IncreaseAmount": "3.66%"},
                  {"ID": "600519", "Name": "乙", "IncreaseAmount": "-1%"}]
                 if params["Index"] == 0 else [{"ID": "300750", "Name": "丙"}]),
    })
    result = await kaipanla.fetch_dataset("ext_kpl_lhb_list", DAY, {"st": 2})
    assert [call[2]["Index"] for call in calls] == [0, 2]
    assert all(call[0] == "POST" for call in calls)
    assert all(not {"Token", "UserID"} & call[2].keys() for call in calls)
    assert len(result["rows"]) == 3
    assert result["rows"][0]["IncreaseAmount_pct"] == "3.66"
    assert result["data_date"] == DAY.isoformat() and result["date_origin"] == "response"


async def test_all_five_limit_tiers_are_collected(monkeypatch):
    calls = fake_request(monkeypatch, lambda params: {"errcode": "0", "info": [[], DAY.isoformat()]})
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
    fake_request(monkeypatch, lambda params: {"errcode": "0", "Total": 1, "info": [[
        [f"00000{params['PidType']}", "甲", *([0] * (row_width - 2))],
    ], DAY.isoformat()]})
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
    a, b = await asyncio.gather(*(
        kaipanla.fetch_dataset("ext_kpl_limit_counts", DAY) for _ in range(2)
    ))
    assert len(calls) == 1
    a["rows"][0]["SJZT"] = "999"
    assert b["rows"][0]["SJZT"] == "52"
    assert (await kaipanla.fetch_dataset("ext_kpl_limit_counts", DAY))["rows"][0]["SJZT"] == "52"


async def test_emotion_history_keeps_source_sessions_and_excludes_future(monkeypatch):
    calls = fake_request(monkeypatch, lambda params: {"errcode": "0", "info": [
        {"Date": "2026-10-02", "strong": 90, "ztjs": 70},
        {"Date": DAY.isoformat(), "strong": 56, "ztjs": 32},
        {"Date": "2026-09-29", "strong": 60, "ztjs": 48},
    ]})
    result = await kaipanla.fetch_dataset("ext_kpl_emotion", DAY, include_history=True)
    assert result["rows"][0]["strong"] == 56
    assert [row["trade_date"] for row in result["history"]] == ["2026-09-29", DAY.isoformat()]
    assert result["history"][0]["date"] == "2026-09-29"
    result["history"][0]["strong"] = 999
    again = await kaipanla.fetch_dataset("ext_kpl_emotion", DAY, include_history=True)
    assert again["history"][0]["strong"] == 60 and len(calls) == 1


async def test_emotion_history_discovers_latest_without_relabelling_holiday(monkeypatch):
    fake_request(monkeypatch, lambda params: {"errcode": "0", "info": [
        {"Date": DAY.isoformat(), "strong": 56, "ztjs": 32},
    ]})
    result = await kaipanla.fetch_dataset("ext_kpl_emotion", date(2026, 10, 1), include_history=True)
    assert result["state"] == "empty" and result["rows"] == []
    assert result["history"][0]["trade_date"] == DAY.isoformat()
    assert result["data_date"] == "2026-10-01"
    with pytest.raises(ValueError, match="未返回请求日期"):
        await kaipanla.fetch_dataset("ext_kpl_emotion", date(2026, 10, 1))


async def test_history_mode_rejects_other_datasets():
    with pytest.raises(ValueError, match="情绪"):
        await kaipanla.fetch_dataset("ext_kpl_limit_counts", DAY, include_history=True)


async def test_repeated_page_rejected_before_publication(monkeypatch):
    fake_request(monkeypatch, lambda params: {"errcode": "0", "Total": 5,
                                           "list": [{"ID": "000001", "Name": "甲"}]})
    with pytest.raises(ValueError, match="分页未推进"):
        await kaipanla.fetch_dataset("ext_kpl_lhb_list", DAY, {"st": 1})
    assert not kaipanla._CACHE


async def test_max_pages_rejects_partial_result(monkeypatch):
    fake_request(monkeypatch, lambda params: {"errcode": "0", "Total": 10,
        "list": [{"ID": str(params["Index"]), "Name": "甲"}]})
    with pytest.raises(ValueError, match="安全上限"):
        await kaipanla.fetch_dataset("ext_kpl_lhb_list", DAY, {"st": 1})
    assert not kaipanla._CACHE


async def test_early_empty_page_with_positive_total_is_rejected(monkeypatch):
    fake_request(monkeypatch, lambda params: {"errcode": "0", "Total": 3,
        "list": [{"ID": "000001", "Name": "甲"}] if params["Index"] == 0 else []})
    with pytest.raises(ValueError, match="提前返回空页"):
        await kaipanla.fetch_dataset("ext_kpl_lhb_list", DAY)
    assert not kaipanla._CACHE


async def test_vendor_page_cap_does_not_truncate_unknown_total(monkeypatch):
    calls = fake_request(monkeypatch, lambda params: {"errcode": "0", "list": (
        [{"ID": f"00000{params['Index']}", "Name": "甲"}] if params["Index"] < 2 else []
    )})
    result = await kaipanla.fetch_dataset("ext_kpl_lhb_list", DAY, {"st": 100})
    assert len(result["rows"]) == 2
    assert [call[2]["Index"] for call in calls] == [0, 1, 2]


async def test_new_high_group_total_rejects_early_empty_page(monkeypatch):
    fake_request(monkeypatch, lambda params: {"errcode": "0", "GroupCount": 3,
        "GroupList": [{"GroupID": "1", "GroupName": "甲", "List": []}]
        if params["Index"] == 0 else []})
    with pytest.raises(ValueError, match="提前返回空页"):
        await kaipanla.fetch_dataset("ext_kpl_new_high", DAY)
    assert not kaipanla._CACHE


async def test_latest_news_is_a_bounded_snapshot(monkeypatch):
    today = date(2026, 10, 1)
    calls = fake_request(monkeypatch, lambda params: {"errcode": "0", "List": [
        {"ID": "latest", "Time": "1790812800", "Content": "最新消息"},
    ]})
    result = await kaipanla.fetch_dataset("ext_kpl_news", today)
    assert len(calls) == 1 and calls[0][2]["Index"] == "0"
    assert calls[0][2]["st"] == 20
    assert len(result["rows"]) == 1
    assert result["data_date"] is None and result["date_origin"] == "observation"


async def test_broken_limits_numeric_total_uses_offset_and_stops(monkeypatch):
    spec = get_dataset("ext_kpl_broken_limits")
    width = len(spec.columns)
    calls = fake_request(monkeypatch, lambda params: {"errcode": "0", "info": [[
        [f"00000{params['Index']}", "甲", *([0] * (width - 2))],
    ], 2]})
    result = await kaipanla.fetch_dataset(spec.id, DAY, {"PidType": 1})
    assert len(result["rows"]) == 2
    assert [call[2]["Index"] for call in calls] == [0, 1]


@pytest.mark.parametrize(("dataset_id", "parameters", "payload"), [
    ("ext_kpl_lhb_details", {"StockID": "600825"}, {"Name": "新华传媒", "Time": DAY.isoformat(), "List": []}),
    ("ext_kpl_theme_details", {"ID": "2955"}, {"Table": [], "StockList": [], "Stocks": []}),
])
async def test_details_bind_explicit_request_identity(monkeypatch, dataset_id, parameters, payload):
    fake_request(monkeypatch, lambda params: {"errcode": "0", **payload})
    result = await kaipanla.fetch_dataset(dataset_id, DAY if dataset_id.endswith("lhb_details") else date(2026, 10, 1), parameters)
    assert result["rows"][0]["ID"] == next(iter(parameters.values()))


async def test_detail_conflicting_identity_is_rejected(monkeypatch):
    fake_request(monkeypatch, lambda params: {"errcode": "0", "ID": "other", "Table": []})
    with pytest.raises(ValueError, match="标识"):
        await kaipanla.fetch_dataset("ext_kpl_theme_details", date(2026, 10, 1), {"ID": "2955"})


@pytest.mark.parametrize("parameters", [{"Token": "x"}, {"UserID": 1}, {"c": "Theme"}])
async def test_credentials_and_controller_override_are_rejected(parameters):
    with pytest.raises(ValueError):
        await kaipanla.fetch_dataset("ext_kpl_limit_counts", DAY, parameters)


async def test_current_only_dataset_rejects_historical_query():
    with pytest.raises(ValueError, match="当前快照"):
        await kaipanla.fetch_dataset("ext_kpl_news", DAY)


async def test_range_cannot_cross_historical_target(monkeypatch):
    calls = fake_request(monkeypatch, lambda params: {})
    with pytest.raises(ValueError, match="区间结束日期"):
        await kaipanla.fetch_dataset("ext_kpl_range_stocks", DAY, {
            "DStart": "2026-09-20", "DEnd": "2026-10-01",
        })
    assert not calls


def test_extension_preview_uses_anonymous_adapter(tmp_path, monkeypatch):
    from app.api.ext_data import router as ext_router
    from app.services.ext_data import ExtConfigStore

    monkeypatch.setattr(kaipanla, "cn_today", lambda: DAY)
    monkeypatch.setattr("app.api.ext_data.cn_today", lambda: DAY)
    calls = fake_request(monkeypatch, lambda params: {"errcode": "0", "info": [1, 2, 3, 4, 5]})
    config = preset("limit_ladder_counts")
    ExtConfigStore(tmp_path).upsert(config)
    app = FastAPI()
    app.include_router(ext_router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    with TestClient(app) as client:
        response = client.post(f"/api/ext-data/{config.id}/pull/test")
        assert response.status_code == 200 and response.json()["total_rows"] == 1
        assert not list(tmp_path.rglob("*.parquet"))
        calls.clear()
        config.pull.headers["Cookie"] = "forbidden"
        ExtConfigStore(tmp_path).upsert(config)
        assert client.post(f"/api/ext-data/{config.id}/pull/test").status_code == 400
        assert not calls


async def test_wrong_day_never_overwrites_last_complete_snapshot(tmp_path, monkeypatch):
    config = preset("limit_ladder_counts")
    rows_to_parquet([{"record_id": "old", "first_board_count": "99", "date": DAY.isoformat()}],
                    config, tmp_path, DAY, replace=True)
    fake_request(monkeypatch, lambda params: {"errcode": "0", "Date": "2026-09-29", "info": [1, 2, 3, 4, 5]})
    with pytest.raises(ValueError, match="日期"):
        await ext_pull.fetch_and_ingest(config, tmp_path, DAY)
    saved = pl.read_parquet(tmp_path / "ext_data" / config.id / "timeseries" / f"date={DAY}" / "part.parquet")
    assert saved["first_board_count"].to_list() == ["99"]


async def test_successful_empty_snapshot_clears_old_rows(tmp_path, monkeypatch):
    config = preset("limit_ladder")
    rows_to_parquet([{"StockID": "600519", "Name": "已退出", "date": DAY.isoformat()}],
                    config, tmp_path, DAY, replace=True)
    fake_request(monkeypatch, lambda params: {"errcode": "0", "Date": DAY.isoformat(), "StockList": []})
    count, actual = await ext_pull.fetch_and_ingest(config, tmp_path, DAY)
    saved = pl.read_parquet(tmp_path / "ext_data" / config.id / "timeseries" / f"date={DAY}" / "part.parquet")
    assert count == 0 and actual == DAY.isoformat() and saved.is_empty()
    assert "StockID" in saved.columns


def test_replace_does_not_change_generic_merge_semantics(tmp_path):
    config = ExtConfig("custom", "自定义", "timeseries", [ExtField("key"), ExtField("value")], market_level=True)
    rows_to_parquet([{"key": "a", "value": "old"}], config, tmp_path, DAY)
    rows_to_parquet([{"key": "b", "value": "new"}], config, tmp_path, DAY)
    path = tmp_path / "ext_data" / "custom" / "timeseries" / f"date={DAY}" / "part.parquet"
    assert pl.read_parquet(path).height == 2
    rows_to_parquet([{"key": "b", "value": "new"}], config, tmp_path, DAY, replace=True)
    assert pl.read_parquet(path)["key"].to_list() == ["b"]


async def test_history_without_date_parameter_does_not_leak_future(monkeypatch):
    fake_request(monkeypatch, lambda params: {"errcode": "0", "x": [
        "20260929_100_10_0", "20260930_110_15_0", "20261001_120_20_0",
    ]})
    result = await kaipanla.fetch_dataset("ext_kpl_new_high_trend", DAY)
    assert [row["trend_date"] for row in result["rows"]] == ["2026-09-29", "2026-09-30"]


def test_api_catalog_and_query_parameter_failures(tmp_path):
    app = FastAPI()
    app.include_router(router, prefix="/api/market-recap")
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    with TestClient(app) as client:
        catalog = client.get("/api/market-recap/kaipanla/catalog").json()
        assert len(catalog["datasets"]) == 39 and len(catalog["excluded"]) == 4
        assert client.get("/api/market-recap/kaipanla/context?date=2026-09-30").json()["state"] == "no_data"
        assert client.post("/api/market-recap/kaipanla/query/ext_kpl_limit_counts",
                           json={"date": DAY.isoformat(), "parameters": {"Token": "x"}}).status_code == 400
        assert client.post("/api/market-recap/kaipanla/query/ext_kpl_theme_details",
                           json={"parameters": {}}).status_code == 400
        assert client.post("/api/market-recap/kaipanla/query/ext_kpl_unknown", json={}).status_code == 404


def test_api_refresh_keeps_other_tables_and_previous_snapshot(tmp_path, monkeypatch):
    old = preset("limit_ladder_counts")
    rows_to_parquet([{"first_board_count": "99", "date": DAY.isoformat()}],
                    old, tmp_path, DAY, replace=True)
    def response(params):
        if params["a"] == "DailyLimitIndex":
            raise TimeoutError("temporary upstream timeout")
        if params["a"] == "MarketCapacity":
            return {"errcode": "0", "info": {"Date": DAY.isoformat(), "last": "123"}}
        raise TimeoutError("unavailable")
    fake_request(monkeypatch, response)
    app = FastAPI()
    app.include_router(router, prefix="/api/market-recap")
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    with TestClient(app) as client:
        result = client.post("/api/market-recap/kaipanla/refresh", json={"date": DAY.isoformat()})
        assert result.status_code == 200
        body = result.json()
        assert len(body["results"]) == 8
        assert next(item for item in body["results"] if item["id"] == "ext_kpl_capacity")["state"] == "ok"
        assert any(item["state"] == "error" for item in body["results"])
        context = client.get(f"/api/market-recap/kaipanla/context?date={DAY}").json()
        assert context["date"] == DAY.isoformat() and context["state"] == "partial"
        assert next(item for item in context["tables"] if item["id"] == "ext_kpl_capacity")["rows"][0]["last_wan"] == "123"
    # The failed refresh never touches unrelated saved partitions.
    path = tmp_path / "ext_data" / old.id / "timeseries" / f"date={DAY}" / "part.parquet"
    assert pl.read_parquet(path)["first_board_count"].to_list() == ["99"]


def test_api_query_success_and_network_failure(tmp_path, monkeypatch):
    fake_request(monkeypatch, lambda params: {"errcode": "0", "info": {"SJZT": "52", "SJDT": "10"}})
    app = FastAPI()
    app.include_router(router, prefix="/api/market-recap")
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    with TestClient(app) as client:
        result = client.post("/api/market-recap/kaipanla/query/ext_kpl_limit_counts", json={"date": DAY.isoformat()})
        assert result.status_code == 200 and result.json()["rows"][0]["SJZT"] == "52"
        assert not list(tmp_path.rglob("*.parquet"))
        kaipanla._CACHE.clear()
        async def failed(*args):
            raise TimeoutError("unavailable")
        monkeypatch.setattr(kaipanla, "_request", failed)
        assert client.post("/api/market-recap/kaipanla/query/ext_kpl_limit_counts", json={"date": DAY.isoformat()}).status_code == 502
