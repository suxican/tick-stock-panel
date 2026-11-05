from copy import deepcopy

import httpx
import pytest

from app.plugins.zzshare import ZzshareError
from app.plugins.zzshare.provider import ZzshareProvider

DAY = "2026-11-04"


def topic(tid, name, score, scope):
    return {
        "id": tid, "name": name, "score": score, "selection_scope": scope, "today_pct": 7.9,
        "up_count": 11, "stock_count": 11, "total_count": 11, "up_ratio": 100, "leader_count": 9,
        "limit_up_count": 1, "limit_down_count": 0, "mainline": {"tag": "趋势共振", "continuity_score": 69.1},
        "core_stocks": [{"code": "603728", "name": "鸣志电器", "role": "领涨先锋", "pct": 5.9,
                         "price": 60.49, "core_score": 49.6, "quote_date": DAY, "quote_time": DAY + " 15:00"}],
    }


def snapshot():
    return {
        "trade_date": DAY, "source": "historical_score", "cache_status": "historical",
        "is_trading": False, "market_snapshot_at": "15:00", "calculated_at": DAY + "T15:00:13",
        "candidate_count": 139, "strategy_candidate_count": 10,
        "selection_rule": "shared_strategy_top2_plus_all_top1",
        "items": [topic(95, "人形机器人/Figure", 98.2, "strategy"),
                  topic(18, "特斯拉人形机器人", 94.5, "strategy"),
                  topic(105, "华为韬定律", 100, "all")],
    }


def source(payload, *, status=200):
    calls = []

    def handle(request):
        calls.append((request.url.path, dict(request.url.params)))
        return httpx.Response(status, json={"code": 20000, "data": payload})

    return ZzshareProvider(transport=httpx.MockTransport(handle)), calls


def test_home_top3_keeps_official_order_scopes_and_core_stocks():
    p, calls = source(snapshot())
    result = p.home_topics(DAY)
    assert calls == [("/v3/topic/home-topics", {"date": DAY})]
    assert result["date"] == DAY and result["market_snapshot_at"] == "15:00"
    assert result["source_asof"] == DAY + "T15:00:13+08:00"
    assert result["candidate_count"] == 139 and result["strategy_candidate_count"] == 10
    assert result["selection_rule"] == "shared_strategy_top2_plus_all_top1"
    assert [(r["tid"], r["score"], r["scope"]) for r in result["items"]] == [
        ("95", 98.2, "strategy"), ("18", 94.5, "strategy"), ("105", 100, "all")]
    row = result["items"][0]
    assert row["rising_pct"] == 100 and row["avg_change_pct"] == 7.9
    assert row["valid_count"] == row["member_count"] == row["rising_count"] == 11
    assert row["strong_count"] == 9 and row["limit_up_count"] == 1 and row["limit_down_count"] == 0
    assert row["mainline_tag"] == "趋势共振" and row["continuity_score"] == 69.1
    assert row["core_stocks"] == [{"symbol": "603728.SH", "name": "鸣志电器", "role": "领涨先锋",
                                    "core_score": 49.6, "change_pct": 5.9, "price_yuan": 60.49}]


def test_live_and_revised_historical_keep_actual_date_and_source_time():
    payload = snapshot()
    payload.update(source="realtime", cache_status="ready", calculated_at=DAY + "T07:00:09.610537Z")
    p, calls = source(payload)
    result = p.home_topics(None)
    assert calls == [("/v3/topic/home-topics", {})]
    assert result["date"] == DAY and result["source"] == "realtime" and result["cache_status"] == "ready"
    assert result["source_asof"] == DAY + "T15:00:09.610537+08:00"
    payload.update(calculated_at="2026-11-12T05:10:20", source="historical_score", cache_status="historical")
    payload["items"][0]["core_stocks"] = []
    result = source(payload)[0].home_topics(DAY)
    assert result["source_asof"] == "2026-11-12T05:10:20+08:00"
    assert result["items"][0]["core_stocks"] == []


def test_empty_holiday_and_empty_eligible_set_are_distinguishable():
    data = snapshot()
    data.update(items=[], candidate_count=0, strategy_candidate_count=0, calculated_at=None)
    holiday = source(data)[0].home_topics(DAY)
    assert holiday["source_asof"] is None and holiday["items"] == []
    data.update(candidate_count=133, calculated_at=DAY + "T19:33:16")
    closed = source(data)[0].home_topics(DAY)
    assert closed["candidate_count"] == 133 and closed["items"] == [] and closed["source_asof"]


def test_official_close_can_publish_just_one_seat():
    payload = snapshot()
    only = topic(137, "大消费/乳业", 33.3, "all")
    only["core_stocks"] = []
    payload.update(trade_date="2026-10-26", calculated_at="2026-10-26T19:32:56",
                   candidate_count=136, strategy_candidate_count=0, items=[only])
    result = source(payload)[0].home_topics("2026-10-26")
    assert result["source"] == "historical_score" and result["market_snapshot_at"] == "15:00"
    assert result["strategy_candidate_count"] == 0
    assert [(r["tid"], r["scope"]) for r in result["items"]] == [("137", "all")]


@pytest.mark.parametrize("case", ["wrong_day", "wrong_core_day", "wrong_core_stamp", "duplicate",
                                  "missing_asof", "bad_score", "bad_count", "missing_items"])
def test_malformed_or_stale_official_rows_are_not_published(case):
    data = snapshot()
    first = data["items"][0]
    if case == "wrong_day":
        data["trade_date"] = "2026-11-03"
    elif case == "wrong_core_day":
        first["core_stocks"][0]["quote_date"] = "2026-11-03"
    elif case == "wrong_core_stamp":
        first["core_stocks"][0]["quote_time"] = "2026-11-03 15:00"
    elif case == "duplicate":
        data["items"][1] = deepcopy(first)
    elif case == "missing_asof":
        data["calculated_at"] = None
    elif case == "bad_score":
        first["score"] = True
    elif case == "bad_count":
        first["stock_count"] += 1
    else:
        del data["items"]
    with pytest.raises(ZzshareError):
        source(data)[0].home_topics(DAY)


def test_official_history_calendar_and_failures():
    dates = ["2026-11-03", DAY]
    p, calls = source(dates)
    assert p.home_topic_dates() == [DAY, "2026-11-03"]
    assert calls == [("/v3/topic/home-topics/history-dates", {"limit": "60"})]
    for malformed in ([DAY, DAY], ["2026-02-31"], None):
        with pytest.raises(ZzshareError):
            source(malformed)[0].home_topic_dates()
    with pytest.raises(ZzshareError) as err:
        source(None, status=429)[0].home_topics(None)
    assert err.value.status_code == 429
    p, calls = source(None)
    with pytest.raises(ValueError):
        p.home_topics("20261104")
    assert calls == []
