"""Supplier dates, isolated failures, raw units, and shared-cache dashboard contracts."""
from __future__ import annotations

import asyncio
import copy
import json
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.kaipanla import router
from app.market_time import CN_TZ
from app.services import kaipanla, market_emotion

TODAY = date(2026, 10, 2)
DAY = date(2026, 9, 30)
PREVIOUS = date(2026, 9, 29)
FETCHED = "2026-10-02T09:20:00+08:00"
PREFIX = "/api/market-recap/kaipanla"
CURRENT = {
    "emotion", "capacity", "indices", "limit_counts", "limit_ladder_counts",
    "limit_performance", "withdrawal", "weight", "live", "breadth",
}


@pytest.fixture(autouse=True)
def isolated_source(monkeypatch):
    kaipanla._CACHE.clear()
    monkeypatch.setattr(kaipanla, "cn_today", lambda: TODAY)
    monkeypatch.setattr(market_emotion, "cn_today", lambda: TODAY)

    async def forbidden_network(*args, **kwargs):
        raise AssertionError("tests must never issue a real supplier request")

    monkeypatch.setattr(kaipanla, "_request", forbidden_network)
    yield
    kaipanla._CACHE.clear()


def stats():
    return {**{str(key): abs(key) + 10 for key in range(-10, 11)},
            "SZJS": 2567, "XDJS": 2823, "ZT": 56, "DT": 12, "SJZT": 52, "SJDT": 9}


def live_row(day=DAY, *, hour=15, info=None):
    return {"ID": "93809", "Time": int(datetime.combine(day, datetime.min.time(), CN_TZ).replace(hour=hour).timestamp()),
            "Comment": "供应商盘面解读", "ShareData_json": json.dumps({"ZDTJ_info": stats() if info is None else info})}


def source_rows(name, day):
    return {
        "emotion": [{"strong": "56", "ztjs": "52", "lbgd": "7", "df_num": "8"}],
        "capacity": [{"last_wan": "123456.5", "s_zrcs_wan": "135000", "yclnstr": "预测成交额原文"}],
        "indices": [{"StockID": "SH000001", "prod_name": "上证指数", "last_px": "3800.25",
                     "increase_rate_pct": "0.36", "turnover": "123456789"}],
        "limit_counts": [{"SJZT": "52", "SJDT": "9"}],
        "limit_ladder_counts": [{"first_board_count": "40", "second_board_count": "7",
                                "third_board_count": "2", "fourth_board_count": "1", "fifth_plus_count": "2"}],
        "limit_performance": [{"limit_up_count": "52", "two_board_count": "7", "three_board_count": "2",
                               "max_board_count": "7", "two_board_promotion_pct": "0.36",
                               "broken_rate_pct": "19.35", "summary": "源数据总结"}],
        "withdrawal": [{"StockID": "600001", "Name": "甲", "change_pct": "-1.5",
                        "withdrawal_pct": "-9.25", "price": "10.5"}],
        "weight": [{"plate_id": "1", "plate_name": "银行", "change_pct": "0.6",
                    "leader_id": "600001", "leader_name": "甲", "leader_pct": "0.8"}],
        "live": [live_row(day)],
    }[name]


def result(dataset_id, day, *, rows=None, history=False):
    name = dataset_id.removeprefix("ext_kpl_")
    if rows is None:
        rows = source_rows(name, day) if day <= DAY else []
    out = {"id": dataset_id, "source": "开盘啦", "requested_date": day.isoformat(),
           "data_date": day.isoformat(), "date_origin": "response", "fetched_at": FETCHED,
           "state": "ok" if rows else "empty", "rows": [{**row, "date": day.isoformat()} for row in rows]}
    if history:
        out["history"] = [{"trade_date": session.isoformat(), **source_rows("emotion", session)[0]}
                          for session in (PREVIOUS, DAY) if session <= day]
    return out


def fake_source(monkeypatch, transform=None):
    calls = []

    async def fetch(dataset_id, day=None, parameters=None, *, force=False, include_history=False):
        calls.append((dataset_id, day, parameters, force, include_history))
        out = result(dataset_id, day, history=include_history)
        return transform(dataset_id, day, out) if transform else out

    monkeypatch.setattr(kaipanla, "fetch_dataset", fetch)
    return calls


def api_client():
    app = FastAPI()
    app.include_router(router, prefix="/api/market-recap")
    # The dashboard must also work without a local quote repository.
    return TestClient(app)


async def test_default_uses_proven_supplier_day_and_previous_session_once(monkeypatch):
    calls = fake_source(monkeypatch)
    out = await market_emotion.get_market_emotion()
    assert out["requested_date"] is None and out["date"] == str(DAY)
    assert out["previous_date"] == str(PREVIOUS) and out["state"] == "ok"
    assert set(out["datasets"]) == CURRENT | {"previous_limit_counts", "previous_limit_performance"}
    assert calls[0] == ("ext_kpl_emotion", TODAY, None, False, True)
    assert len(calls) == 11 and sum(call[0] == "ext_kpl_emotion" for call in calls) == 1
    assert all(call[1] == (PREVIOUS if index > 8 else DAY) for index, call in enumerate(calls[1:], 1))
    assert not any(call[0] in {"ext_kpl_limit_up", "ext_kpl_broken_limits"} for call in calls)
    assert out["datasets"]["emotion"]["rows"][0]["strong"] == 56
    assert out["datasets"]["emotion"]["date_origin"] == "response"
    assert out["fetched_at"] == FETCHED


async def test_explicit_holiday_does_not_relabel_earlier_history(monkeypatch):
    calls = fake_source(monkeypatch)
    out = await market_emotion.get_market_emotion(TODAY)
    assert out["date"] == str(TODAY) and out["requested_date"] == str(TODAY)
    assert out["state"] == "empty" and out["previous_date"] is None
    assert all(call[1] == TODAY for call in calls) and len(calls) == 9
    assert all(section["rows"] == [] for section in out["datasets"].values())
    assert out["history"]["rows"][-1]["trade_date"] == str(DAY)


@pytest.mark.parametrize("failed", [False, True])
async def test_unconfirmed_default_never_fetches_guessed_today(monkeypatch, failed):
    def transform(dataset_id, day, out):
        if failed:
            raise TimeoutError("https://secret/?Token=private")
        return {**out, "history": [], "rows": [], "state": "empty"}

    calls = fake_source(monkeypatch, transform)
    out = await market_emotion.get_market_emotion()
    assert out["date"] is None and out["previous_date"] is None and len(calls) == 1
    assert out["state"] == ("error" if failed else "empty")
    assert out["fetched_at"] == (None if failed else FETCHED)
    assert "private" not in json.dumps(out)


async def test_failures_are_independent_safe_and_have_no_fabricated_zero_or_time(monkeypatch):
    def transform(dataset_id, day, out):
        if dataset_id == "ext_kpl_capacity":
            raise TimeoutError("supplier failure Token=private&UserID=123")
        return out

    fake_source(monkeypatch, transform)
    out = await market_emotion.get_market_emotion(DAY, force=True)
    failed = out["datasets"]["capacity"]
    assert out["state"] == "partial" and failed["state"] == "error"
    assert failed["rows"] == [] and failed["fetched_at"] is None
    assert out["datasets"]["limit_counts"]["rows"][0]["SJZT"] == 52
    assert out["fetched_at"] == FETCHED and "private" not in json.dumps(out)


async def test_explicit_day_keeps_other_supplier_metrics_when_history_fails(monkeypatch):
    def transform(dataset_id, day, out):
        if dataset_id == "ext_kpl_emotion":
            raise TimeoutError("emotion unavailable")
        return out

    calls = fake_source(monkeypatch, transform)
    out = await market_emotion.get_market_emotion(DAY)
    assert out["date"] == str(DAY) and out["previous_date"] is None
    assert out["history"]["state"] == "error" and out["datasets"]["emotion"]["state"] == "error"
    assert out["datasets"]["indices"]["state"] == "ok" and out["state"] == "partial"
    assert all(call[1] == DAY for call in calls) and len(calls) == 9


@pytest.mark.parametrize("root_mismatch", [True, False])
async def test_other_day_component_is_rejected_without_leaking_values(monkeypatch, root_mismatch):
    def transform(dataset_id, day, out):
        if dataset_id == "ext_kpl_indices":
            if root_mismatch:
                out["data_date"] = str(PREVIOUS)
            else:
                out["rows"][0]["date"] = str(PREVIOUS)
            out["rows"][0]["last_px"] = "999999"
        return out

    fake_source(monkeypatch, transform)
    out = await market_emotion.get_market_emotion(DAY)
    assert out["datasets"]["indices"]["state"] == "error"
    assert out["datasets"]["indices"]["rows"] == [] and "999999" not in json.dumps(out)
    assert out["datasets"]["emotion"]["state"] == "ok"


async def test_numeric_units_preserve_supplier_percent_wan_and_yuan(monkeypatch):
    fake_source(monkeypatch)
    out = await market_emotion.get_market_emotion(DAY)
    indices = out["datasets"]["indices"]
    assert indices["rows"][0]["increase_rate_pct"] == 0.36
    assert indices["rows"][0]["turnover"] == 123456789
    assert out["datasets"]["capacity"]["rows"][0]["last_wan"] == 123456.5
    assert {col["name"]: col["unit"] for col in indices["columns"]}["turnover"] == "yuan"
    assert next(col for col in indices["columns"] if col["name"] == "increase_rate_pct")["unit"] == "percent"
    assert next(col for col in out["datasets"]["capacity"]["columns"] if col["name"] == "last_wan")["unit"] == "wan"
    assert out["datasets"]["limit_performance"]["rows"][0]["yesterday_limit_up_pct"] is None


async def test_live_stocks_merge_source_lists_without_extra_requests(monkeypatch):
    focus = [["000710", " 贝瑞基因 ", 10.02], ["603538", "美诺华", "10"],
             ["688185", "康希诺", None], ["000710", "贝瑞基因", 10.02]]
    discussion = [["000710", "贝瑞基因"], ["688185", "康希诺", "20"],
                  ["920001", "讨论股票"], ["600383", "金地集团", -1.25]]

    def transform(dataset_id, day, out):
        if dataset_id == "ext_kpl_live":
            out["rows"][0].update(Stock_json=json.dumps(focus), DisStock_json=json.dumps(discussion))
        return out

    calls = fake_source(monkeypatch, transform)
    row = (await market_emotion.get_market_emotion(DAY))["datasets"]["live"]["rows"][0]
    assert row["stocks"] == [
        {"symbol": "000710", "name": "贝瑞基因", "change_pct": 10.02, "kind": "focus"},
        {"symbol": "603538", "name": "美诺华", "change_pct": 10, "kind": "focus"},
        {"symbol": "688185", "name": "康希诺", "change_pct": 20, "kind": "focus"},
        {"symbol": "920001", "name": "讨论股票", "change_pct": None, "kind": "discussion"},
        {"symbol": "600383", "name": "金地集团", "change_pct": -1.25, "kind": "discussion"},
    ]
    assert json.loads(row["Stock_json"]) == focus and json.loads(row["DisStock_json"]) == discussion
    assert len(calls) == 11


@pytest.mark.parametrize("invalid", [None, "", "not-json", "{}", '"text"', "null", "true"])
async def test_invalid_live_stock_json_does_not_hide_valid_message_or_other_list(monkeypatch, invalid):
    def transform(dataset_id, day, out):
        if dataset_id == "ext_kpl_live":
            out["rows"][0].update(Stock_json=invalid, DisStock_json='[["600383","金地集团"]]')
        return out

    fake_source(monkeypatch, transform)
    section = (await market_emotion.get_market_emotion(DAY))["datasets"]["live"]
    assert section["state"] == "ok"
    assert section["rows"][0]["Comment"] == "供应商盘面解读"
    assert section["rows"][0]["stocks"] == [
        {"symbol": "600383", "name": "金地集团", "change_pct": None, "kind": "discussion"},
    ]


@pytest.mark.parametrize("invalid", ["nan", "inf", "-inf", "invalid", "1%", True, None])
async def test_live_stock_nonfinite_or_invalid_percent_is_missing(monkeypatch, invalid):
    def transform(dataset_id, day, out):
        if dataset_id == "ext_kpl_live":
            out["rows"][0]["Stock_json"] = json.dumps([["600383", "金地集团", invalid]])
        return out

    fake_source(monkeypatch, transform)
    stock = (await market_emotion.get_market_emotion(DAY))["datasets"]["live"]["rows"][0]["stocks"][0]
    assert stock["change_pct"] is None


async def test_live_stock_invalid_entries_are_isolated_and_comments_never_infer_stocks(monkeypatch):
    invalid = [None, {}, "600383", [], ["600383"], [600383, "数字代码", 1],
               ["\uff16\uff10\uff10\uff13\uff18\uff13", "全角代码", 1], ["SH600383", "前缀代码", 1],
               ["600383.SH", "后缀代码", 1], ["60038", "短代码", 1],
               ["600383", None, 1], ["600383", "  ", 1], ["../383", "路径代码", 1]]

    def transform(dataset_id, day, out):
        if dataset_id == "ext_kpl_live":
            out["rows"][0].update(Comment="贵州茅台600519涨超2%", Stock_json=json.dumps(invalid))
        return out

    fake_source(monkeypatch, transform)
    row = (await market_emotion.get_market_emotion(DAY))["datasets"]["live"]["rows"][0]
    assert row["stocks"] == [] and row["Comment"] == "贵州茅台600519涨超2%"


@pytest.mark.parametrize("value", ["nan", "inf", "invalid", True, None, -1, 1.5])
async def test_invalid_counts_are_missing_instead_of_zero(monkeypatch, value):
    def transform(dataset_id, day, out):
        if dataset_id == "ext_kpl_limit_counts":
            out["rows"][0]["SJZT"] = value
            out["rows"][0]["SJDT"] = "0"
        return out

    fake_source(monkeypatch, transform)
    rows = (await market_emotion.get_market_emotion(DAY))["datasets"]["limit_counts"]["rows"]
    assert rows[0]["SJZT"] is None and rows[0]["SJDT"] == 0


async def test_history_is_bounded_sorted_and_previous_is_supplier_session(monkeypatch):
    sessions = [DAY - timedelta(days=days) for days in range(90) if (DAY - timedelta(days=days)).weekday() < 5]
    def transform(dataset_id, day, out):
        if "history" in out:
            out["history"] = [{"trade_date": session.isoformat(), "strong": "50"} for session in sessions]
            out["history"].append({"trade_date": "2026-10-03", "strong": "999"})
        return out

    fake_source(monkeypatch, transform)
    out = await market_emotion.get_market_emotion(DAY)
    history_dates = [row["trade_date"] for row in out["history"]["rows"]]
    assert len(history_dates) == 60 and history_dates == sorted(history_dates)
    assert max(history_dates) == str(DAY) and out["previous_date"] == str(PREVIOUS)
    assert "999" not in json.dumps(out["history"])


async def test_breadth_uses_latest_complete_same_day_message_and_raw_bins(monkeypatch):
    def transform(dataset_id, day, out):
        if dataset_id == "ext_kpl_live":
            incomplete = stats()
            del incomplete["-10"]
            out["rows"] = [live_row(DAY, hour=14), live_row(PREVIOUS, hour=23),
                           live_row(DAY, hour=16, info=incomplete), live_row(DAY)]
        return out

    fake_source(monkeypatch, transform)
    breadth = (await market_emotion.get_market_emotion(DAY))["datasets"]["breadth"]
    row = breadth["rows"][0]
    assert breadth["state"] == "ok" and row["published_at"] == "2026-09-30T15:00:00+08:00"
    assert row["source_time"] == 1790751600 and row["up_count"] == 2567
    assert row["limit_up_count"] == 56 and row["actual_limit_up_count"] == 52
    assert row["limit_down_count"] == 12 and row["actual_limit_down_count"] == 9
    assert len(row["bins"]) == 21 and row["bins"][10] == {"key": "0", "label": "档位 0", "count": 10}
    assert "桶边界" in breadth["message"]


@pytest.mark.parametrize("invalid", ["other_day", "missing", "negative", "nonfinite"])
async def test_unverified_breadth_is_empty_not_reconstructed(monkeypatch, invalid):
    def transform(dataset_id, day, out):
        if dataset_id == "ext_kpl_live":
            info = stats()
            if invalid == "missing":
                del info["SZJS"]
            if invalid == "negative":
                info["SJDT"] = -1
            if invalid == "nonfinite":
                info["0"] = "nan"
            out["rows"] = [live_row(PREVIOUS if invalid == "other_day" else day, info=info)]
        return out

    fake_source(monkeypatch, transform)
    out = await market_emotion.get_market_emotion(DAY)
    assert out["datasets"]["live"]["state"] == "ok"
    assert out["datasets"]["breadth"]["state"] == "empty"
    assert out["datasets"]["breadth"]["rows"] == [] and out["state"] == "partial"


async def test_aggregation_reuses_client_singleflight_cache_and_concurrency(monkeypatch):
    calls, active, peak = [], 0, 0

    async def upstream(spec, day, parameters, *, include_history=False):
        nonlocal active, peak
        calls.append((spec.id, day, include_history))
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.002)
        active -= 1
        return result(spec.id, day, history=include_history)

    monkeypatch.setattr(kaipanla, "_fetch", upstream)
    first, second = await asyncio.gather(*(market_emotion.get_market_emotion() for _ in range(2)))
    assert peak == 2 and len(calls) == len(set(calls)) == 11
    first["datasets"]["emotion"]["rows"][0]["strong"] = 999
    assert second["datasets"]["emotion"]["rows"][0]["strong"] == 56
    again = await market_emotion.get_market_emotion()
    assert len(calls) == 11 and again["datasets"]["emotion"]["rows"][0]["strong"] == 56
    await market_emotion.get_market_emotion(force=True)
    assert len(calls) == 22 and peak == 2


@pytest.mark.parametrize(("empty", "failed", "expected"), [(False, False, "ok"), (True, False, "empty"), (False, True, "error")])
def test_api_normal_empty_error_and_force_without_local_repository(monkeypatch, empty, failed, expected):
    def transform(dataset_id, day, out):
        if failed:
            raise TimeoutError("secret upstream detail")
        if empty:
            out.update(rows=[], state="empty", history=[])
        return out

    calls = fake_source(monkeypatch, transform)
    with api_client() as client:
        response = client.get(f"{PREFIX}/market-emotion?date={DAY}&force=true")
    assert response.status_code == 200 and response.json()["state"] == expected
    assert calls and all(call[3] is True for call in calls)
    assert "secret upstream detail" not in response.text


def test_api_future_and_invalid_parameters_fail_before_supplier_calls(monkeypatch):
    calls = fake_source(monkeypatch)
    with api_client() as client:
        assert client.get(f"{PREFIX}/market-emotion?date=2026-10-03").status_code == 400
        assert client.get(f"{PREFIX}/market-emotion?date=not-a-date").status_code == 422
        assert client.get(f"{PREFIX}/market-emotion?force=not-bool").status_code == 422
    assert calls == []


@pytest.mark.parametrize("force", [None, True])
def test_lazy_stock_query_keeps_wrapper_and_passes_optional_force(monkeypatch, force):
    calls = []
    expected = {"id": "ext_kpl_broken_limits", "source": "开盘啦", "rows": [], "state": "empty"}

    async def query(dataset_id, day, parameters, *, force=False):
        calls.append((dataset_id, day, copy.deepcopy(parameters), force))
        return expected

    monkeypatch.setattr(kaipanla, "fetch_dataset", query)
    body = {"date": str(DAY), "parameters": {"PidType": 1}}
    if force is not None:
        body["force"] = force
    with api_client() as client:
        response = client.post(f"{PREFIX}/query/ext_kpl_broken_limits", json=body)
    assert response.status_code == 200 and response.json() == expected
    assert calls == [("ext_kpl_broken_limits", DAY, {"PidType": 1}, force or False)]


async def test_missing_features_are_explicit_and_never_derived(monkeypatch):
    fake_source(monkeypatch)
    out = await market_emotion.get_market_emotion(DAY)
    missing = {item["id"]: item for item in out["missing"]}
    assert {"independent_seal_rate", "full_broken_list", "separate_promotion"} <= missing.keys()
    assert all(item["label"] and item["reason"] for item in missing.values())
    assert "seal_rate" not in out["datasets"]["limit_performance"]["rows"][0]


def test_api_returns_typed_live_stocks_and_explicit_intraday_gap(monkeypatch):
    def transform(dataset_id, day, out):
        if dataset_id == "ext_kpl_live":
            out["rows"][0]["Stock_json"] = '[["000710","贝瑞基因",10.02]]'
        return out

    fake_source(monkeypatch, transform)
    with api_client() as client:
        response = client.get(f"{PREFIX}/market-emotion?date={DAY}")
    assert response.status_code == 200
    out = response.json()
    assert out["datasets"]["live"]["rows"][0]["stocks"] == [
        {"symbol": "000710", "name": "贝瑞基因", "change_pct": 10.02, "kind": "focus"},
    ]
    gap = next(item for item in out["missing"] if item["id"] == "index_intraday")
    assert gap["label"] == "指数分时图" and "分钟序列" in gap["reason"]
