from copy import deepcopy
from datetime import datetime

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.first_board_attention import AttentionEvaluator
from app.services.first_board_priority import candidate_priority, expire_priority
from app.strategy.first_board import FirstBoardRules


def row():
    group = {"id": "hot", "name": "设备", "peer_rank": 1, "peer_total": 2,
             "peer_mean_change_pct": .052, "peer_market_mean_change_pct": .037,
             "peer_up_ratio": .75, "peer_coverage": .95, "stock_group_rank": 1,
             "peer_valid_count": 19, "peer_count": 20, "follow": False, "peer_persistent": False}
    return {"state": "approaching", "can_buy": True, "price": 10.9, "change_pct": .09,
            "distance_to_limit_pct": .01, "turnover_rate": 8., "evidence": {
                "breakout_price_raw": 10., "amount": 100_000_000.},
            "attention_context": {"groups": [group], "intraday_rising": True}}


def priority(candidate, radar=None, **kwargs):
    return candidate_priority(candidate, rules=kwargs.pop("rules", FirstBoardRules()), radar=radar,
                              live=kwargs.pop("live", True), **kwargs)


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
    assert scored["parts"]["stock"]["points"] == pytest.approx(36.7)
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
