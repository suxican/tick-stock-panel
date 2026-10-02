"""开盘啦匿名数据目录与金融边界,不发送真实请求。"""
from __future__ import annotations

import json
from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest

from app.services.kaipanla_catalog import datasets, get_dataset, normalize_payload, presets

DAY = date(2026, 9, 30)


def _spec(name):
    spec = get_dataset(f"ext_kpl_{name}")
    assert spec is not None
    return spec


def test_catalog_is_anonymous_unique_and_excludes_auth_ambiguity():
    specs = datasets()
    assert len(specs) == 39
    assert len({s.id for s in specs}) == len(specs)
    assert not {"GetBKJJ_W36", "GetBKJJBL", "GetFengKListBest", "MorningBiddingList"} & {
        s.action for s in specs
    }
    assert all(not {"Token", "UserID"} & s.params.keys() for s in specs)
    assert get_dataset("../../secrets") is None


def test_presets_keep_user_control_and_are_market_level():
    configs = presets()
    assert configs
    assert {c.id for c in configs} == {s.id for s in datasets() if not s.required_params}
    for config in configs:
        assert config.market_level is True
        assert config.mode == "timeseries"
        assert config.pull is not None and config.pull.enabled is False
        assert {f.name for f in config.fields} >= {"date", "source", "fetched_at"}
        assert config.fields[0].name not in {"date", "source", "fetched_at"}
        assert not {"Token", "UserID"} & parse_qs(urlsplit(config.pull.url).query).keys()


def test_indices_follow_the_four_system_indices():
    query = parse_qs(urlsplit(next(c for c in presets() if c.id == "ext_kpl_indices").pull.url).query)
    assert query["StockIDList"] == ["SH000001,SZ399001,SZ399006,SH000680"]
    assert "SH000688" not in query["StockIDList"][0]


def test_emotion_filters_multi_day_history_to_requested_day():
    payload = {"errcode": "0", "info": [
        {"Day": "2026-10-01", "strong": "65", "ztjs": "70"},
        {"Day": "2026-09-30", "strong": "42", "ztjs": "38"},
    ]}
    rows, actual, count = normalize_payload(_spec("emotion"), payload, DAY)
    assert actual == DAY and count == 2
    assert len(rows) == 1 and rows[0]["strong"] == "42"
    assert rows[0]["ztjs"] == "38"


def test_emotion_missing_requested_day_is_not_wrong_day():
    rows, actual, count = normalize_payload(_spec("emotion"), {
        "info": [{"Day": "2026-09-29", "strong": "60"}], "errcode": "0",
    }, DAY)
    assert (rows, actual, count) == ([], None, 1)


@pytest.mark.parametrize("key,value", [
    ("Date", "2026-09-29"), ("date", "20260929"),
    ("Day", ["2026-09-29"]), ("day", "2026-09-29"), ("Time", "2026-09-29"),
])
def test_response_date_mismatch_rejected(key, value):
    with pytest.raises(ValueError, match="日期"):
        normalize_payload(_spec("limit_ladder_counts"), {"info": [1, 2, 3, 4, 5], key: value}, DAY)


def test_conflicting_date_aliases_rejected():
    with pytest.raises(ValueError, match="日期"):
        normalize_payload(_spec("limit_ladder_counts"), {
            "info": [1, 2, 3, 4, 5], "Date": "2026-09-30", "date": "2026-09-29",
        }, DAY)


def test_embedded_limit_list_date_and_extra_array_fields():
    row = ["002726", "龙大美食", 0, "", 1776302700, "农业", 39182180,
           275723072, 91895163, 141993242, -50098079, 158897958, "猪肉、农业",
           2351136421, 6.76, 1, 0, 1.52, "", "801464", 1, 3.62, 10.03, "新增字段"]
    rows, actual, count = normalize_payload(_spec("limit_up"), {
        "info": [[row], "2026-09-30"], "errcode": "0",
    }, DAY)
    assert actual == DAY and count == 1
    assert rows[0]["StockID"] == "002726"
    assert rows[0]["change_pct"] == 10.03  # 保留供应商百分数,不映射 realtime
    assert json.loads(rows[0]["extra_json"]) == ["新增字段"]


def test_limit_array_missing_required_positions_is_rejected():
    with pytest.raises(ValueError, match=r"字段|结构"):
        normalize_payload(_spec("limit_up"), {"info": [[["002726"]], DAY.isoformat()]}, DAY)


def test_broken_limit_numeric_count_envelope_is_not_a_date():
    stock = ["301529", "福赛科技", 0, "", 115.82, 0.42, "汽车零部件", 1000,
             2000, -1000, 3000, 4000, 5.5, 0, 16.2, "", 0, 5000]
    rows, actual, count = normalize_payload(_spec("broken_limits"), {
        "info": [[stock], 11], "errcode": "0",
    }, DAY)
    assert actual is None and count == 1
    assert rows[0]["StockID"] == "301529" and rows[0]["max_seal_amount"] == 5000


def test_empty_broken_limit_numeric_envelope_is_empty_page():
    assert normalize_payload(_spec("broken_limits"), {"info": [[], 11]}, DAY) == ([], None, 0)


def test_direct_two_broken_rows_are_not_mistaken_for_envelope():
    stock = ["301529", "福赛科技", 0, "", 115.82, 0.42, "汽车零部件", 1000,
             2000, -1000, 3000, 4000, 5.5, 0, 16.2, "", 0, 5000]
    rows, actual, count = normalize_payload(_spec("broken_limits"), {"info": [stock, stock]}, DAY)
    assert actual is None and count == 2 and len(rows) == 2


def test_range_stock_missing_only_trailing_reserved_field_is_accepted():
    stock = ["301190", "善水科技", 0, 44.01, 649323523, -565794335, 83529188,
             46.3461, 1103155090, 2760621007, "分散染料、农业", 1, "游资", 2, 0]
    rows, actual, count = normalize_payload(_spec("range_stocks"), {"List": [stock]}, DAY)
    assert actual is None and count == 1
    assert rows[0]["StockID"] == "301190" and rows[0]["inflow_days"] == 2
    assert rows[0]["reserved14"] == 0 and rows[0]["reserved15"] is None
    assert _spec("range_stocks").params["st"] == "1000"


def test_range_stock_missing_trailing_business_field_is_rejected():
    stock = ["002580", "圣阳股份", 30.58, 90.06, 7265739404, -7176973197, 88766207,
             272.797, 19903514654, 10630292007, "液冷、算力", 1, "游资"]
    with pytest.raises(ValueError, match="字段"):
        normalize_payload(_spec("range_stocks"), {"List": [stock]}, DAY)


def test_new_high_pagination_counts_groups_not_flattened_stocks():
    stock = ["300866", "安克创新", 120.68, 7.89, "储能", 1517063039, 72383693,
             534340639, -461956946, 25826238239, 64703661000, 1, "801178", "储能", 6.02]
    rows, actual, count = normalize_payload(_spec("new_high"), {
        "Date": DAY.isoformat(), "GroupList": [
            {"GroupID": "801178", "GroupName": "储能", "List": [stock, stock]},
            {"GroupID": "801001", "GroupName": "芯片", "List": [stock]},
        ],
    }, DAY)
    assert actual == DAY and count == 2 and len(rows) == 3
    assert rows[0]["GroupName"] == "储能"


def test_nested_live_data_is_json_and_record_identity_is_stable():
    payload = {"date": DAY.isoformat(), "List": [{
        "ID": "88877", "Time": 1790751600, "Comment": "指数上涨",
        "ShareData": {"ZDTJ_info": {"SZJS": "3000"}}, "Stock": [["600664", "哈药股份"]],
    }]}
    rows, actual, count = normalize_payload(_spec("live"), payload, DAY)
    assert actual == DAY and count == 1 and rows[0]["ID"] == "88877"
    assert json.loads(rows[0]["ShareData_json"])["ZDTJ_info"]["SZJS"] == "3000"


def test_current_index_percentage_and_unknown_fields_remain_visible():
    rows, actual, count = normalize_payload(_spec("indices"), {"StockList": [{
        "StockID": "SH000680", "prod_name": "科创综指", "last_px": "1000.00",
        "increase_rate": "-0.70%", "increase_amount": "-7.00", "turnover": 80000000,
        "vendor_new": {"extra": "保留"},
    }]}, DAY)
    assert actual is None and count == 1
    assert rows[0]["increase_rate_pct"] == "-0.70"
    assert json.loads(rows[0]["extra_json"]) == {"vendor_new": {"extra": "保留"}}


def test_limit_ladder_keeps_sector_details_and_has_no_symbol_mapping():
    payload = {"Date": DAY.isoformat(), "StockList": [[
        "002580", "圣阳股份", 6, 1776303444, "801807", "算力", 0, 1, 20, 9663901824, 36273385216,
    ]], "ZhuShuList": [["801807", "算力", 20, 36273385216, "002580,000815"]]}
    rows, actual, count = normalize_payload(_spec("limit_ladder"), payload, DAY)
    assert actual == DAY and count == 1
    assert json.loads(rows[0]["ZhuShuList_json"])[0][1] == "算力"
    assert "symbol" not in rows[0]


def test_lhb_list_differentiates_one_day_and_three_day_records():
    payload = {"Time": DAY.isoformat(), "list": [
        {"ID": "002361", "Name": "神剑股份", "D3": "0", "IncreaseAmount": "3.88%"},
        {"ID": "002361", "Name": "神剑股份", "D3": "1", "IncreaseAmount": "13.88%"},
    ]}
    rows, actual, count = normalize_payload(_spec("lhb_list"), payload, DAY)
    assert actual == DAY and count == 2
    assert rows[0]["record_id"] != rows[1]["record_id"]
    assert rows[0]["IncreaseAmount_pct"] == "3.88"


def test_lhb_detail_seat_date_mismatch_is_rejected():
    payload = {"Time": DAY.isoformat(), "ID": "002361", "Name": "神剑股份", "List": [{
        "BuyList": [{"Day": "2026-09-29", "StockID": "002361", "Buy": "1000"}],
        "SellList": [],
    }], "OnTimeList": ["2026-09-29"]}
    with pytest.raises(ValueError, match="日期"):
        normalize_payload(_spec("lhb_details"), payload, DAY)


def test_lhb_detail_historical_date_list_is_valid_reference():
    payload = {"Time": DAY.isoformat(), "ID": "002361", "Name": "神剑股份", "List": [{
        "BuyList": [{"Day": DAY.isoformat(), "StockID": "002361", "Buy": "1000"}],
        "SellList": [],
    }], "OnTimeList": ["2026-09-29", "2026-09-28"]}
    rows, actual, _count = normalize_payload(_spec("lhb_details"), payload, DAY)
    assert actual == DAY
    assert json.loads(rows[0]["OnTimeList_json"]) == ["2026-09-29", "2026-09-28"]


def test_weight_performance_flattens_direction_without_colliding_key():
    payload = {"info": {"SZ": [["881155", "银行", 0.3, "601665", "齐鲁银行", 1.16]],
                        "XD": [["881155", "银行", -0.3, "601665", "齐鲁银行", 1.16]]}}
    rows, actual, count = normalize_payload(_spec("weight"), payload, DAY)
    assert actual is None and count == 2
    assert rows[0]["direction"] == "SZ" and rows[1]["direction"] == "XD"
    assert rows[0]["record_id"] != rows[1]["record_id"]


def test_theme_search_preserves_main_and_subthemes_with_distinct_keys():
    payload = {"List": [{"ID": "9", "Name": "光刻机概念", "CreateTime": "1698997269"}],
               "SList": [{"ID": "9", "Name": "光模块", "LName": ["光模块"], "LID": ["4217"]}]}
    rows, actual, count = normalize_payload(_spec("theme_search"), payload, DAY)
    assert actual is None and count == 1 and len(rows) == 2
    assert json.loads(rows[1]["LName_json"]) == ["光模块"]
    assert rows[0]["record_id"] != rows[1]["record_id"]


def test_capacity_conserves_documented_unit_and_source_date():
    payload = {"info": {"last": "234167807", "s_zrcs": "241523238", "s_zrtj": "241523238",
                        "s3_zrtj": "231643107", "yclnstr": "23417亿", "time": 1776519147,
                        "date": DAY.isoformat()}}
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
        normalize_payload(_spec("limit_ladder_counts"), {"info": [1, 2, 3, 4, 5], "Date": "yesterday"}, DAY)


def test_investor_stock_code_and_nested_seats_are_preserved():
    payload = {"Time": DAY.isoformat(), "DongXiang": [{"ID": "9", "ShortName": "某游资", "List": [{
        "ID": 967, "Name": "盈峰环境", "Money": 1000, "D3": 0, "IncreaseAmount": "0.00%",
        "GInfo": ["0:1:15490"], "Slist": [{"BuyList": [{"Day": DAY.isoformat(), "Buy": "1000"}], "SellList": []}],
    }]}]}
    rows, actual, count = normalize_payload(_spec("hot_money_flows"), payload, DAY)
    assert actual == DAY and count == 1
    assert rows[0]["StockID"] == "000967" and rows[0]["investor_id"] == "9"
    assert rows[0]["IncreaseAmount_pct"] == "0.00"
    assert json.loads(rows[0]["Slist_json"])[0]["BuyList"][0]["Buy"] == "1000"


def test_lhb_money_unit_remains_explicitly_raw():
    rows, actual, count = normalize_payload(_spec("lhb_flows"), {
        "Date": DAY.isoformat(), "List": [{"BName": "某游资", "BID": 41,
        "Buy": [{"Sto": "000967", "StoN": "盈峰环境", "Money": 114520000, "Three": 1}],
        "Sell": []}],
    }, DAY)
    assert actual == DAY and count == 1
    assert rows[0]["money_raw"] == 114520000
    assert "amount_yuan" not in rows[0]


def test_limit_reasons_flatten_group_and_keep_distinct_business_keys():
    stock = ["002333", "罗普斯金", 0, "", 0, 0, 1776319068, 0, 9528065,
             "首板", 1, "光伏", 110355427, 377858516, 28.91, 1357767755, "光伏", "详情", 0]
    rows, actual, count = normalize_payload(_spec("limit_reasons"), {
        "date": DAY.isoformat(), "list": [
            {"ZSCode": "801807", "ZSName": "算力", "StockList": [stock]},
            {"ZSCode": "801178", "ZSName": "储能", "StockList": [stock]},
        ],
    }, DAY)
    assert actual == DAY and count == 2 and len(rows) == 2
    assert rows[0]["reason"] == "光伏"
    assert rows[0]["record_id"] != rows[1]["record_id"]


def test_holidays_are_reference_dates_not_trading_calendar():
    rows, actual, count = normalize_payload(_spec("holidays"), {
        "List": ["2026-01-01", "2026-10-01"],
    }, DAY)
    assert actual is None and count == 2
    assert rows == [{"holiday_date": "2026-01-01"}, {"holiday_date": "2026-10-01"}]


def test_new_high_trend_keeps_its_own_dates():
    rows, actual, count = normalize_payload(_spec("new_high_trend"), {
        "x": ["20200102_412_138_0", "20200103_429_84_0"],
    }, DAY)
    assert actual is None and count == 2
    assert rows[0]["trend_date"] == "2020-01-02"
    assert rows[0]["new_high_total"] == "412"
    assert "date" not in rows[0]


def test_plate_history_keeps_each_business_day_without_claiming_current_snapshot():
    stock = ["002333", "罗普斯金", 0, "", 0, 0, 1776319068, 0, 9528065,
             "首板", 1, "光伏", 110355427, 377858516, 28.91, 1357767755, "光伏", "详情", 0]
    rows, actual, count = normalize_payload(_spec("plate_history"), {
        "ZSCode": "801807", "ZSName": "算力", "list": [
            {"Date": "2026-09-29", "ZSName": "算力", "StockList": [stock]},
            {"Date": "2026-09-30", "ZSName": "算力", "StockList": [stock]},
        ],
    }, DAY)
    assert actual is None and count == 2
    assert [row["trade_date"] for row in rows] == ["2026-09-29", "2026-09-30"]
    assert rows[0]["record_id"] != rows[1]["record_id"]


@pytest.mark.parametrize("name", ["stock_plates", "stock_plates_v2", "review_list", "theme_details", "theme_search", "news"])
def test_current_only_datasets_cannot_claim_history(name):
    assert _spec(name).historical is False


@pytest.mark.parametrize("payload", [None, [], {"errcode": "1001", "info": [1, 2, 3, 4, 5]}, {"info": {"wrong": []}}])
def test_structure_and_upstream_error_fail_closed(payload):
    with pytest.raises(ValueError):
        normalize_payload(_spec("limit_ladder_counts"), payload, DAY)
