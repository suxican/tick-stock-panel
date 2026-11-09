from copy import deepcopy
from datetime import datetime

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.first_board_attention import AttentionEvaluator
from app.services.first_board_priority import candidate_priority, expire_priority, session_minutes
from app.strategy.first_board import FirstBoardRules

NOW = datetime(2026, 11, 5, 10, tzinfo=CN_TZ)


def row():
    group = {"id": "hot", "name": "设备", "peer_rank": 1, "peer_total": 2,
             "peer_mean_change_pct": .052, "peer_market_mean_change_pct": .037,
             "peer_up_ratio": .75, "peer_coverage": .95, "stock_group_rank": 1,
             "peer_valid_count": 19, "peer_count": 20, "follow": False, "peer_persistent": False}
    # 开盘30分钟累计1.8万手, 前5日日均7.2万手: 量比2。
    return {"symbol": "600001.SH", "state": "approaching", "can_buy": True, "price": 10.9, "change_pct": .09,
            "distance_to_limit_pct": .01, "turnover_rate": 8., "evidence": {
                "breakout_price_raw": 10., "amount": 100_000_000., "volume": 18_000., "avg_volume_5": 72_000.,
                "first_approach_at": "2026-11-05T09:45:00+08:00"},
            "attention_context": {"groups": [group], "intraday_rising": True}}


def priority(candidate, radar=None, **kwargs):
    return candidate_priority(candidate, rules=kwargs.pop("rules", FirstBoardRules()), radar=radar,
                              live=kwargs.pop("live", True), now=kwargs.pop("now", NOW), **kwargs)


def test_priority_removes_stock_from_every_overlapping_rank_and_benchmark():
    changes = [.20, *[.01] * 5, *[.02] * 5, *[.025] * 6]
    symbols = [f"{n:06d}.SZ" for n in range(len(changes))]
    quotes = pl.DataFrame({"symbol": symbols, "change_pct": changes})
    groups = [{"id": name, "name": name, "source_id": "src", "kind": "concept", "field": "concept",
               "members": members} for name, members in (
        ("A", symbols[:6]), ("B", symbols[:1] + symbols[6:11]), ("C", symbols[11:]))]
    result = AttentionEvaluator().evaluate(quotes, groups, symbols[:1], pl.DataFrame(),
                                          now=datetime(2026, 11, 5, 10, tzinfo=CN_TZ),
                                          membership_version="v1")[symbols[0]]
    a, b = result["groups"]
    assert b["rank"] == 1  # Candidate boosted both groups.
    assert (a["peer_rank"], b["peer_rank"], b["peer_total"]) == (3, 2, 3)
    assert a["peer_market_mean_change_pct"] == pytest.approx(sum(changes[1:]) / 16)
    assert b["peer_mean_change_pct"] == pytest.approx(.02)
    assert b["peer_coverage"] == 1 and b["peer_valid_count"] == 5
    assert not b["peer_strong"]
    smaller = deepcopy(groups)
    smaller[0]["members"].pop()
    groups = AttentionEvaluator().evaluate(quotes, smaller, symbols[:1], pl.DataFrame(),
                                          now=datetime(2026, 11, 5, 10, tzinfo=CN_TZ),
                                          membership_version="v2")[symbols[0]]["groups"]
    assert groups[0]["peer_strong"] is None and groups[1]["peer_total"] == 2


def test_weight_caps_overlapping_groups_and_role_timing():
    candidate = row()
    pop = {"status": "ready", "rank": 4, "rank_gain": 12}
    scored = priority(candidate, {"popularity": pop})
    assert scored["status"] == "ready" and scored["known_max"] == 100 and scored["role"] == "leader"
    assert scored["parts"]["popularity"]["points"] == 10
    assert scored["version"] == "priority-v2"
    assert scored["parts"]["stock"]["points"] == pytest.approx(30.)
    sectors = candidate["attention_context"]["groups"]
    sectors.append({**sectors[0], "id": "dup"})
    assert priority(candidate, {"popularity": pop})["score"] == scored["score"]
    # Only verified radar membership corroborates this stock, capped at two points.
    radar = {"popularity": pop, "theme": {"status": "ready", "tid": 12, "name": "供应商题材"}}
    after = priority(candidate, radar)
    assert after["parts"]["sector"]["points"] == scored["parts"]["sector"]["points"] + 2.
    for sector in sectors:
        sector.update(follow=True, stock_group_rank=3)
    assert priority(candidate)["role"] == "follower"


def test_peer_persistence_needs_actual_observations_same_session_and_uninterrupted_strength():
    symbols = [f"{i:06d}.SZ" for i in range(12)]
    groups = [{"id": name, "name": name, "kind": "concept", "source_id": "src", "field": "concept",
               "members": members} for name, members in (("板块甲", symbols[:6]), ("板块乙", symbols[6:]))]
    engine = AttentionEvaluator()

    def observe(hour, minute, second=0, *, change=.04, version="v1"):
        quotes = pl.DataFrame({"symbol": symbols, "change_pct": [change] * 6 + [0.] * 6})
        now = datetime(2026, 11, 5, hour, minute, second, tzinfo=CN_TZ)
        return engine.evaluate(quotes, groups, symbols[:1], pl.DataFrame(), now=now,
                               membership_version=version)[symbols[0]]["groups"][0]

    first = observe(10, 0)
    assert first["peer_strong"] and first["peer_persistent"] is None
    assert observe(10, 2, 59)["peer_reference_at"] is None
    confirmed = observe(10, 3)
    assert confirmed["peer_persistent"] and confirmed["peer_reference_at"] == "2026-11-05T10:00:00+08:00"
    candidate = row()
    candidate["attention_context"]["groups"] = [first]
    initial = priority(candidate)["parts"]["sector"]
    candidate["attention_context"]["groups"] = [confirmed]
    stable = priority(candidate)["parts"]["sector"]
    assert stable["points"] == initial["points"] + 2
    assert stable["known_max"] == 30 and initial["known_max"] == 28
    assert observe(10, 4, change=-.02)["peer_persistent"] is False
    assert observe(10, 5)["peer_persistent"] is None
    assert observe(10, 8)["peer_persistent"] is True
    assert observe(10, 8, version="changed")["peer_persistent"] is None
    assert observe(10, 11, version="changed")["peer_persistent"] is True
    assert observe(10, 22, version="changed")["peer_persistent"] is None  # 11-minute observation gap
    assert observe(11, 29)["peer_persistent"] is None and observe(13, 0)["peer_persistent"] is None
    assert observe(13, 3)["peer_persistent"] is True


def test_missing_and_unranked_are_distinct_and_missing_never_inflates_points():
    candidate = row()
    empty = priority(candidate)
    assert empty["known_max"] == 90 and empty["parts"]["popularity"]["points"] is None
    known = priority(candidate, {"popularity": {"status": "unranked"}})
    assert known["known_max"] == 100 and known["score"] == empty["score"]
    one = priority(candidate, {"popularity": {"status": "ready", "rank": 12}})
    assert one["known_max"] == 98 and one["score"] == empty["score"] + 7
    candidate["attention_context"]["intraday_rising"] = None
    incomplete = priority(candidate)
    assert incomplete["parts"]["timing"]["points"] is None and incomplete["known_max"] == 70
    assert incomplete["score"] == pytest.approx(empty["score"] - 20)
    candidate["attention_context"]["groups"] = []
    assert priority(candidate)["known_max"] == 40
    external = {"plate": {"status": "ready", "source": "vendor_plate", "tid": "12"},
                "theme": {"status": "ready", "source": "official_core", "tid": "99"}}
    only_external = priority(candidate, external)
    assert only_external["known_max"] == 42 and only_external["primary_sector"] is None
    assert only_external["parts"]["sector"]["points"] == 2 and only_external["radar"] == external
    external["plates"] = [dict(external["plate"], plate_type=kind) for kind in (17, 15, 14)]
    multi = priority(candidate, external)
    assert multi["score"] == only_external["score"]
    assert multi["radar"]["plates"] == external["plates"]


@pytest.mark.parametrize("state,live,expected", [
    ("sealed", True, "sealed"), ("broken", True, "broken"), ("invalid", True, "invalid"),
    ("approaching", False, "stale"),
])
def test_score_withdrawn_for_non_actionable_terminal_or_stale_rows(state, live, expected):
    candidate = row() | {"state": state}
    answer = priority(candidate, live=live)
    assert answer["status"] == expected and answer["score"] is None and not answer["can_buy"]
    fresh = priority(row())
    expire_priority(fresh, "行情已过期")
    assert fresh["status"] == "stale" and not fresh["radar"] and fresh["score"] is None


def test_watch_shadow_and_blocked_never_grant_permission():
    candidate = row() | {"can_buy": False}
    assert priority(candidate)["status"] == "blocked" and priority(candidate)["score"] is None
    shadow = priority(candidate | {"sector_shadow_eligible": True})
    assert shadow["status"] == "shadow" and shadow["score"] and not shadow["can_buy"]
    watching = priority(candidate | {"state": "watch"})
    assert watching["score"] and watching["status"] == "watch" and not watching["can_buy"]
    assert priority(candidate | {"state": "watch", "change_pct": .045})["score"] is None
    assert priority(candidate | {"state": "watch", "turnover_rate": None})["score"] is None
    assert priority(candidate | {"state": "watch"}, rules=FirstBoardRules(approaching_distance=0))["score"]


def test_stock_part_ranks_proximity_volume_ratio_and_first_approach_time():
    candidate = row()
    stock = priority(candidate)["parts"]["stock"]
    # 接近度 12.5/15 + 量比2 得 7.5/15 + 开盘后15分钟临板 10/10。
    assert stock["points"] == pytest.approx(30.) and stock["known_max"] == 40
    assert "量比 2.00" in stock["reason"] and "首次临板为开盘后 15 分钟" in stock["reason"]
    # 时段分取首次临板时刻, 不随盯盘时间衰减; 量比按当前已进行分钟数重算。
    later = priority(candidate, now=datetime(2026, 11, 5, 13, 30, tzinfo=CN_TZ))["parts"]["stock"]
    assert later["points"] == pytest.approx(22.5) and later["known_max"] == 40
    candidate["evidence"]["first_approach_at"] = "2026-11-05T13:50:00+08:00"
    late = priority(candidate, now=datetime(2026, 11, 5, 14, tzinfo=CN_TZ))["parts"]["stock"]
    assert late["points"] == pytest.approx(14.7)


@pytest.mark.parametrize("evidence", [{"volume": None}, {"avg_volume_5": 0.}, {"volume": float("nan")}])
def test_missing_volume_baseline_lowers_known_max_instead_of_scoring_zero(evidence):
    candidate = row()
    candidate["evidence"].update(evidence)
    stock = priority(candidate)["parts"]["stock"]
    assert stock["known_max"] == 25 and stock["points"] == pytest.approx(22.5) and "量比待确认" in stock["reason"]


def test_opening_minutes_and_unverifiable_approach_time_stay_unknown():
    candidate = row()
    candidate["evidence"]["first_approach_at"] = "2026-11-05T09:31:00+08:00"
    opening = priority(candidate, now=datetime(2026, 11, 5, 9, 34, tzinfo=CN_TZ))["parts"]["stock"]
    assert opening["known_max"] == 25 and "量比待确认" in opening["reason"]
    for stamp in ("2026-11-04T09:45:00+08:00", "2026-11-05T10:01:00+08:00", "2026-11-05T09:45:00", "bad"):
        candidate["evidence"]["first_approach_at"] = stamp
        stock = priority(candidate)["parts"]["stock"]
        assert stock["known_max"] == 30 and "临板时刻待确认" in stock["reason"]


def test_twenty_percent_boards_use_their_own_proximity_threshold():
    candidate = row() | {"symbol": "300001.SZ", "change_pct": .164, "distance_to_limit_pct": .03}
    default = priority(candidate)["parts"]["stock"]["points"]
    wide = priority(candidate, rules=FirstBoardRules(approaching_distance_20cm=.06))["parts"]["stock"]["points"]
    # 接近度 7.5→11.25(四舍五入到一位小数), 量比与时段分不变。
    assert default == pytest.approx(25.) and wide == pytest.approx(28.8)
    main = row() | {"distance_to_limit_pct": .03}
    assert priority(main, rules=FirstBoardRules(approaching_distance_20cm=.06))["parts"]["stock"]["points"] == default


def test_twenty_percent_boards_use_their_own_minimum_change():
    wide = row() | {"symbol": "688001.SH", "change_pct": .12}
    assert priority(wide)["score"] is None and priority(wide)["reason"] == "尚未达到最低涨幅"
    assert priority(wide, rules=FirstBoardRules(min_change_pct_20cm=.12))["score"] is not None
    main = row() | {"change_pct": .06}
    assert priority(main, rules=FirstBoardRules(min_change_pct_20cm=.20))["score"] is not None


@pytest.mark.parametrize(("clock", "expected"), [
    ((9, 29), None), ((9, 30), 0), ((11, 30), 120), ((12, 15), 120), ((13, 0), 120), ((14, 0), 180), ((15, 30), 240),
])
def test_session_minutes_exclude_lunch_break(clock, expected):
    assert session_minutes(datetime(2026, 11, 5, *clock, tzinfo=CN_TZ)) == expected
