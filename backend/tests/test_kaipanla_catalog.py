"""Read-only market-emotion supplier contract regressions migrated from can_main."""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.data_providers.kaipanla_catalog import datasets, get_dataset, normalize_payload

DAY = date(2026, 9, 30)


def _spec(name):
    spec = get_dataset(f"ext_kpl_{name}")
    assert spec is not None
    return spec


def test_catalog_is_anonymous_unique_and_excludes_auth_ambiguity():
    specs = datasets()
    assert len(specs) == 11
    assert len({s.id for s in specs}) == len(specs)
    assert not {"GetBKJJ_W36", "GetBKJJBL", "GetFengKListBest", "MorningBiddingList"} & {
        s.action for s in specs
    }
    assert all(not {"Token", "UserID"} & s.params.keys() for s in specs)
    assert get_dataset("../../secrets") is None


def test_emotion_filters_multi_day_history_to_requested_day():
    payload = {
        "errcode": "0",
        "info": [
            {"Day": "2026-10-01", "strong": "65", "ztjs": "70"},
            {"Day": "2026-09-30", "strong": "42", "ztjs": "38"},
        ],
    }
    rows, actual, count = normalize_payload(_spec("emotion"), payload, DAY)
    assert actual == DAY and count == 2
    assert len(rows) == 1 and rows[0]["strong"] == "42"
    assert rows[0]["ztjs"] == "38"


def test_emotion_missing_requested_day_is_not_wrong_day():
    rows, actual, count = normalize_payload(
        _spec("emotion"),
        {
            "info": [{"Day": "2026-09-29", "strong": "60"}],
            "errcode": "0",
        },
        DAY,
    )
    assert (rows, actual, count) == ([], None, 1)


@pytest.mark.parametrize(
    "key,value",
    [
        ("Date", "2026-09-29"),
        ("date", "20260929"),
        ("Day", ["2026-09-29"]),
        ("day", "2026-09-29"),
        ("Time", "2026-09-29"),
    ],
)
def test_response_date_mismatch_rejected(key, value):
    with pytest.raises(ValueError, match="日期"):
        normalize_payload(_spec("limit_ladder_counts"), {"info": [1, 2, 3, 4, 5], key: value}, DAY)


def test_conflicting_date_aliases_rejected():
    with pytest.raises(ValueError, match="日期"):
        normalize_payload(
            _spec("limit_ladder_counts"),
            {
                "info": [1, 2, 3, 4, 5],
                "Date": "2026-09-30",
                "date": "2026-09-29",
            },
            DAY,
        )


def test_embedded_limit_list_date_and_extra_array_fields():
    row = [
        "002726",
        "龙大美食",
        0,
        "",
        1776302700,
        "农业",
        39182180,
        275723072,
        91895163,
        141993242,
        -50098079,
        158897958,
        "猪肉、农业",
        2351136421,
        6.76,
        1,
        0,
        1.52,
        "",
        "801464",
        1,
        3.62,
        10.03,
        "新增字段",
    ]
    rows, actual, count = normalize_payload(
        _spec("limit_up"),
        {
            "info": [[row], "2026-09-30"],
            "errcode": "0",
        },
        DAY,
    )
    assert actual == DAY and count == 1
    assert rows[0]["StockID"] == "002726"
    assert rows[0]["change_pct"] == 10.03  # 保留供应商百分数,不映射 realtime
    assert json.loads(rows[0]["extra_json"]) == ["新增字段"]


def test_limit_array_missing_required_positions_is_rejected():
    with pytest.raises(ValueError, match=r"字段|结构"):
        normalize_payload(_spec("limit_up"), {"info": [[["002726"]], DAY.isoformat()]}, DAY)


def test_broken_limit_numeric_count_envelope_is_not_a_date():
    stock = [
        "301529",
        "福赛科技",
        0,
        "",
        115.82,
        0.42,
        "汽车零部件",
        1000,
        2000,
        -1000,
        3000,
        4000,
        5.5,
        0,
        16.2,
        "",
        0,
        5000,
    ]
    rows, actual, count = normalize_payload(
        _spec("broken_limits"),
        {
            "info": [[stock], 11],
            "errcode": "0",
        },
        DAY,
    )
    assert actual is None and count == 1
    assert rows[0]["StockID"] == "301529" and rows[0]["max_seal_amount"] == 5000


def test_empty_broken_limit_numeric_envelope_is_empty_page():
    assert normalize_payload(_spec("broken_limits"), {"info": [[], 11]}, DAY) == ([], None, 0)


def test_direct_two_broken_rows_are_not_mistaken_for_envelope():
    stock = [
        "301529",
        "福赛科技",
        0,
        "",
        115.82,
        0.42,
        "汽车零部件",
        1000,
        2000,
        -1000,
        3000,
        4000,
        5.5,
        0,
        16.2,
        "",
        0,
        5000,
    ]
    rows, actual, count = normalize_payload(_spec("broken_limits"), {"info": [stock, stock]}, DAY)
    assert actual is None and count == 2 and len(rows) == 2


def test_nested_live_data_is_json_and_record_identity_is_stable():
    payload = {
        "date": DAY.isoformat(),
        "List": [
            {
                "ID": "88877",
                "Time": 1790751600,
                "Comment": "指数上涨",
                "ShareData": {"ZDTJ_info": {"SZJS": "3000"}},
                "Stock": [["600664", "哈药股份"]],
            }
        ],
    }
    rows, actual, count = normalize_payload(_spec("live"), payload, DAY)
    assert actual == DAY and count == 1 and rows[0]["ID"] == "88877"
    assert json.loads(rows[0]["ShareData_json"])["ZDTJ_info"]["SZJS"] == "3000"


def test_current_index_percentage_and_unknown_fields_remain_visible():
    rows, actual, count = normalize_payload(
        _spec("indices"),
        {
            "StockList": [
                {
                    "StockID": "SH000680",
                    "prod_name": "科创综指",
                    "last_px": "1000.00",
                    "increase_rate": "-0.70%",
                    "increase_amount": "-7.00",
                    "turnover": 80000000,
                    "vendor_new": {"extra": "保留"},
                }
            ]
        },
        DAY,
    )
    assert actual is None and count == 1
    assert rows[0]["increase_rate_pct"] == "-0.70"
    assert json.loads(rows[0]["extra_json"]) == {"vendor_new": {"extra": "保留"}}


def test_weight_performance_flattens_direction_without_colliding_key():
    payload = {
        "info": {
            "SZ": [["881155", "银行", 0.3, "601665", "齐鲁银行", 1.16]],
            "XD": [["881155", "银行", -0.3, "601665", "齐鲁银行", 1.16]],
        }
    }
    rows, actual, count = normalize_payload(_spec("weight"), payload, DAY)
    assert actual is None and count == 2
    assert rows[0]["direction"] == "SZ" and rows[1]["direction"] == "XD"
    assert rows[0]["record_id"] != rows[1]["record_id"]


def test_capacity_conserves_documented_unit_and_source_date():
    payload = {
        "info": {
            "last": "234167807",
            "s_zrcs": "241523238",
            "s_zrtj": "241523238",
            "s3_zrtj": "231643107",
            "yclnstr": "23417亿",
            "time": 1776519147,
            "date": DAY.isoformat(),
        }
    }
    rows, actual, count = normalize_payload(_spec("capacity"), payload, DAY)
    assert actual == DAY and count == 1
    assert rows[0]["last_wan"] == "234167807"
    assert rows[0]["time"] == 1776519147


def test_empty_object_is_no_data_and_null_array_is_invalid():
    assert normalize_payload(_spec("capacity"), {"info": {}}, DAY) == ([], None, 0)
    with pytest.raises(ValueError, match="结构"):
        normalize_payload(_spec("limit_ladder_counts"), {"info": None}, DAY)


def test_object_missing_stable_business_key_is_rejected():
    with pytest.raises(ValueError, match="业务键"):
        normalize_payload(_spec("live"), {"List": [{"Comment": "没有编号"}]}, DAY)


def test_invalid_explicit_date_is_not_treated_as_unknown_date():
    with pytest.raises(ValueError, match="日期"):
        normalize_payload(
            _spec("limit_ladder_counts"), {"info": [1, 2, 3, 4, 5], "Date": "yesterday"}, DAY
        )


@pytest.mark.parametrize(
    "payload", [None, [], {"errcode": "1001", "info": [1, 2, 3, 4, 5]}, {"info": {"wrong": []}}]
)
def test_structure_and_upstream_error_fail_closed(payload):
    with pytest.raises(ValueError):
        normalize_payload(_spec("limit_ladder_counts"), payload, DAY)
