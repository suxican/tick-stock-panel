import polars as pl
import pytest

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


def classify(quotes, groups, shares=None, top_n=1):
    return classify_roles(quotes, groups, shares or {}, {SYMBOL}, core_top_n=top_n)[SYMBOL]


def test_leader_is_first_by_change_and_ties_count_as_first():
    assert classify(frame([.08, .02, .03, .01, 0., -.01]), [group()])["leader"] is True
    assert classify(frame([.08, .08, .03, .01, 0., -.01]), [group()])["leader"] is True
    assert classify(frame([.08, .09, .03, .01, 0., -.01]), [group()])["leader"] is False


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


def test_any_concept_hit_counts_and_matches_name_the_board():
    concept = group([SYMBOL, *[f"30000{i}.SZ" for i in range(1, 6)]], gid="概念A", kind="concept")
    quotes = pl.concat([frame([.08, .09, .03, .01, 0., -.01]), pl.DataFrame({
        "symbol": [f"30000{i}.SZ" for i in range(1, 6)], "change_pct": [.01] * 5,
        "raw_close": [10.0] * 5, "amount": [1e8] * 5})])
    role = classify(quotes, [group(), concept])
    assert role["leader"] is True
    assert [m["name"] for m in role["matches"] if "leader" in m["roles"]] == ["概念A"]


def test_duplicate_or_missing_quotes_are_not_counted():
    quotes = pl.concat([frame([.08, .02, .03, .01, 0., -.01]), frame([.01])])
    assert classify(quotes, [group()])["leader"] is None


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
