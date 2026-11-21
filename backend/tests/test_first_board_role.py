from datetime import datetime, timedelta

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.first_board_role import classify_roles, float_shares_map, role_gate

SYMBOL = "600000.SH"
PEERS = [f"60000{i}.SH" for i in range(1, 6)]


def frame(changes, *, prices=None, amounts=None):
    symbols = [SYMBOL, *PEERS][:len(changes)]
    return pl.DataFrame({
        "symbol": symbols, "change_pct": changes,
        "raw_close": prices or [10.0] * len(changes),
        "amount": amounts or [1e8] * len(changes),
    })


def group(members=None, gid="g1", kind="industry"):
    return {"id": gid, "kind": kind, "name": gid, "members": members or [SYMBOL, *PEERS]}


def classify(quotes, groups, shares=None, top_n=1, **facts):
    return classify_roles(quotes, groups, shares or {}, {SYMBOL}, core_top_n=top_n, **facts)[SYMBOL]


T0 = datetime(2026, 10, 16, 9, 31, 12, tzinfo=CN_TZ)


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def concept(members=None):
    return group(members, gid="概念A", kind="concept")


def test_insufficient_coverage_is_unknown_not_false():
    # 6 名成员仅 4 只有效报价: 覆盖率与样本数均不足。
    role = classify(frame([.08, .09, .03, .01]), [group()])
    assert role["leader"] is None and role["core"] is None
    assert "待确认" in role["reason"]


def test_core_by_float_cap_or_amount_any_hit():
    shares = dict.fromkeys([SYMBOL, *PEERS], 1e8) | {SYMBOL: 5e8}
    changes = [.08, .09, .03, .01, 0., -.01]
    small_amount = [1e7, 9e8, 8e8, 7e8, 6e8, 5e8]
    # 成交额排末位, 但流通市值第一 → 中军。
    assert classify(frame(changes, amounts=small_amount), [group()], shares)["core"] is True
    # 市值不在前列, 成交额第一 → 中军。
    big_amount = [9e9, 1e8, 1e8, 1e8, 1e8, 1e8]
    assert classify(frame(changes, amounts=big_amount), [group()], {})["core"] is True
    # 成交额未进前 1 且缺流通股本 → 待确认, 不判否。
    assert classify(frame(changes, amounts=small_amount), [group()], {})["core"] is None
    # 两个维度都已知且都未进前 1 → 否。
    low_cap = dict.fromkeys([SYMBOL, *PEERS], 1e8) | {SYMBOL: 1e7}
    assert classify(frame(changes, amounts=small_amount), [group()], low_cap)["core"] is False


def test_duplicate_or_missing_quotes_are_not_counted():
    quotes = pl.concat([frame([.08, .02, .03, .01, 0., -.01]), frame([.01])])
    assert classify(quotes, [group()])["core"] is None


def test_no_membership_is_unknown():
    role = classify(frame([.08]), [])
    assert role["leader"] is None and "归属" in role["reason"]


def test_float_shares_map_skips_invalid_rows():
    instruments = pl.DataFrame({"symbol": ["600000.sh", "600001.SH", "600002.SH"],
                                "float_shares": [1e8, None, -1.0]})
    assert float_shares_map(instruments) == {"600000.SH": 1e8}
    assert float_shares_map(pl.DataFrame()) == {}


@pytest.mark.parametrize(("role", "selected", "expected"), [
    ({"leader": False, "core": False}, None, (True, False)),
    ({"leader": True, "core": False}, ["leader"], (True, False)),
    ({"leader": False, "core": True}, ["leader", "core"], (True, False)),
    ({"leader": False, "core": True}, ["leader"], (False, True)),
    ({"leader": None, "core": False}, ["leader", "core"], (False, False)),
])
def test_role_gate(role, selected, expected):
    matched, excluded, reason = role_gate(role, selected)
    assert (matched, excluded) == expected
    assert (reason is None) is matched


def test_leader_is_the_earliest_touch_in_a_concept_with_three_limit_up_members():
    touch = {SYMBOL: at(0), PEERS[0]: at(5), PEERS[1]: at(9)}
    role = classify(frame([.1] * 6), [concept()], touch=touch, touch_reliable=True)
    assert role["leader"] is True
    match = next(m for m in role["matches"] if "leader" in m["roles"])
    assert match["name"] == "概念A" and match["limit_up_count"] == 3 and match["touch_at"] == at(0).isoformat()
    later = {SYMBOL: at(7), PEERS[0]: at(5), PEERS[1]: at(9)}
    assert classify(frame([.1] * 6), [concept()], touch=later, touch_reliable=True)["leader"] is False


def test_two_limit_up_members_is_not_a_board_effect():
    touch = {SYMBOL: at(0), PEERS[0]: at(5)}
    assert classify(frame([.1] * 6), [concept()], touch=touch, touch_reliable=True)["leader"] is False


def test_simultaneous_touch_ties_count_as_leader():
    touch = {SYMBOL: at(3), PEERS[0]: at(3), PEERS[1]: at(9)}
    assert classify(frame([.1] * 6), [concept()], touch=touch, touch_reliable=True)["leader"] is True


def test_opened_members_are_not_limit_up_members():
    # 最早触板的同伴已开板: 剩下 SYMBOL、P2、P3 共 3 只, SYMBOL 最早 → 龙头。
    touch = {SYMBOL: at(5), PEERS[0]: at(0), PEERS[1]: at(6), PEERS[2]: at(7)}
    assert classify(frame([.1] * 6), [concept()], touch=touch, opened={PEERS[0]}, touch_reliable=True)["leader"] is True
    # 候选股自己已开板: 不是龙头。
    earliest = {SYMBOL: at(0), PEERS[0]: at(5), PEERS[1]: at(9), PEERS[2]: at(11)}
    assert classify(frame([.1] * 6), [concept()], touch=earliest, touch_reliable=True)["leader"] is True
    assert classify(frame([.1] * 6), [concept()], touch=earliest, opened={SYMBOL}, touch_reliable=True)["leader"] is False


def test_industry_membership_alone_never_makes_a_leader():
    touch = {SYMBOL: at(0), PEERS[0]: at(5), PEERS[1]: at(9)}
    role = classify(frame([.1] * 6), [group()], touch=touch, touch_reliable=True)
    assert role["leader"] is None and "待确认" in role["reason"]


def test_keys_are_normalized_and_core_only_matches_have_no_touch_details():
    touch = {SYMBOL.lower(): at(0), f" {PEERS[0].lower()} ": at(5), PEERS[1]: at(9)}
    role = classify(frame([.1] * 6, amounts=[9e9, 1e8, 1e8, 1e8, 1e8, 1e8]), [concept(), group(gid="行业B")], touch=touch, touch_reliable=True)
    assert role["leader"] is True
    by_name = {m["name"]: m for m in role["matches"]}
    assert by_name["概念A"]["limit_up_count"] == 3 and by_name["行业B"]["touch_at"] is None
    assert by_name["行业B"]["roles"] == ["core"]


def test_reason_names_why_the_leader_is_unknown_even_when_core_does_not_match():
    low_cap = dict.fromkeys([SYMBOL, *PEERS], 1e8) | {SYMBOL: 1e7}
    role = classify(frame([.1] * 6, amounts=[1e7, 9e8, 8e8, 7e8, 6e8, 5e8]), [concept()], low_cap)
    assert role["leader"] is None and role["core"] is False
    assert "事件流不可用或有缺口，龙头待确认" in role["reason"]


def test_no_membership_reason_is_marked_unconfirmed():
    assert "待确认" in classify(frame([.1]), [])["reason"]


def test_unreliable_stream_or_untouched_candidate_is_unknown():
    touch = {SYMBOL: at(0), PEERS[0]: at(5), PEERS[1]: at(9)}
    unreliable = classify(frame([.1] * 6), [concept()], touch=touch)
    assert unreliable["leader"] is None and "待确认" in unreliable["reason"]
    others = {PEERS[0]: at(5), PEERS[1]: at(9), PEERS[2]: at(11)}
    role = classify(frame([.1] * 6), [concept()], touch=others, touch_reliable=True)
    assert role["leader"] is None and "待确认" in role["reason"]


def test_leader_in_one_concept_is_enough_and_industry_still_counts_for_core():
    touch = {SYMBOL: at(0), PEERS[0]: at(5), PEERS[1]: at(9)}
    other = concept([SYMBOL, *[f"30000{i}.SZ" for i in range(1, 6)]])
    role = classify(frame([.1] * 6), [other, concept()], touch=touch, touch_reliable=True)
    assert role["leader"] is True
    assert classify(frame([.1] * 6, amounts=[9e9, 1e8, 1e8, 1e8, 1e8, 1e8]), [group()])["core"] is True
