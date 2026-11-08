from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import first_board_context as context

REFERENCE = date(2026, 10, 15)
TODAY = date(2026, 10, 16)


@pytest.mark.parametrize("frame,allowed", [
    (pl.DataFrame({"date": [REFERENCE], "state": ["range"]}), True),
    (pl.DataFrame({"date": [TODAY], "state": ["strong"]}), False),
    (pl.DataFrame({"date": [REFERENCE, REFERENCE], "state": ["range", "strong"]}), False),
    (pl.DataFrame({"date": [REFERENCE], "state": ["weak"]}), False),
    (pl.DataFrame({"date": [REFERENCE], "state": ["unknown"]}), False),
    (pl.DataFrame(), False),
])
def test_environment_requires_unique_exact_completed_day(tmp_path, monkeypatch, frame, allowed):
    monkeypatch.setattr(context, "load_regime_history", lambda _: frame)
    result = context.load_environment(tmp_path, as_of=TODAY, reference_date=REFERENCE,
                                      allowed_states=["range", "strong"])
    assert result["allowed"] is allowed
    assert result["date"] == str(REFERENCE)


def test_environment_never_uses_today_or_future(tmp_path, monkeypatch):
    def unexpected(_):
        pytest.fail("Future reference must be rejected before reading history")
    monkeypatch.setattr(context, "load_regime_history", unexpected)
    assert not context.load_environment(tmp_path, as_of=TODAY, reference_date=TODAY,
                                         allowed_states=["strong"])["allowed"]


CANDIDATE = "600999.SH"


def peers_of(quotes, members, candidate=CANDIDATE):
    return context.evaluate_sector_peers(quotes, members, [candidate])[candidate]


def test_sector_confirmation_uses_fraction_units_and_unique_quotes():
    members = dict.fromkeys(["600000.SH", "600001.SH", "600002.SH", CANDIDATE], "金融-银行")
    quotes = pl.DataFrame({"symbol": list(members), "change_pct": [.06, .05, -.01, .09]})
    result = peers_of(quotes, members)
    assert result["confirmed"] and result["count"] == 3 and result["member_count"] == 3
    assert result["mean_change_pct"] == pytest.approx(1 / 30)
    assert result["strong_count"] == 2 and result["up_count"] == 2
    duplicated = pl.concat([quotes, quotes.head(1)])
    result = peers_of(duplicated, members)
    assert not result["confirmed"] and result["count"] == 2


@pytest.mark.parametrize("changes,confirmed", [
    # 旧口径把本股计入: 3 只有效、2 只强势、均涨 3.5% 即可确认。
    ([.08, .06, 0., 0.], False),
    # 本股 +9% 抬高了整体均值; 同行均涨仅 0.625%。
    ([.09, .05, .05, -.04, -.035], False),
    ([.08, .06, .05, 0.], True),
])
def test_sector_confirmation_never_counts_the_candidate_itself(changes, confirmed):
    symbols = [CANDIDATE, *[f"{600000 + i}.SH" for i in range(len(changes) - 1)]]
    members = dict.fromkeys(symbols, "金融-银行")
    result = peers_of(pl.DataFrame({"symbol": symbols, "change_pct": changes}), members)
    assert result["confirmed"] is confirmed
    assert result["excludes_candidate"] is True
    assert result["member_count"] == result["count"] == len(changes) - 1
    assert result["mean_change_pct"] == pytest.approx(sum(changes[1:]) / (len(changes) - 1))
    assert "剔除本股" in result["reason"]


def test_candidate_quote_state_does_not_change_peer_denominator():
    peers = [f"{600000 + i}.SH" for i in range(3)]
    members = dict.fromkeys([*peers, CANDIDATE], "金融-银行")
    peer_quotes = pl.DataFrame({"symbol": peers, "change_pct": [.06, .05, .02]})
    without_own = peers_of(peer_quotes, members)
    with_own = peers_of(pl.concat([peer_quotes, pl.DataFrame({"symbol": [CANDIDATE], "change_pct": [.1]})]),
                        members)
    duplicated_own = peers_of(pl.concat([peer_quotes, pl.DataFrame(
        {"symbol": [CANDIDATE, CANDIDATE], "change_pct": [.1, .1]})]), members)
    for result in (without_own, with_own, duplicated_own):
        assert result["member_count"] == 3 and result["count"] == 3 and result["confirmed"] is True
        assert result["mean_change_pct"] == pytest.approx(.13 / 3)


def test_sole_sector_member_cannot_confirm_itself():
    members = {CANDIDATE: "金融-银行"}
    result = peers_of(pl.DataFrame({"symbol": [CANDIDATE], "change_pct": [.09]}), members)
    assert result["member_count"] == 0 and result["coverage"] == 0
    assert result["confirmed"] is False and "没有其他成员" in result["reason"]


def test_peer_confirmation_is_keyed_by_requested_member_symbols():
    members = {"600000.SH": "金融-银行", "600001.SH": "金融-银行", "000001.SZ": "制造-机械"}
    quotes = pl.DataFrame({"symbol": list(members), "change_pct": [.06, .01, .02]})
    result = context.evaluate_sector_peers(quotes, members, [" 600000.sh ", "600002.SH", "000001.SZ"])
    assert set(result) == {"600000.SH", "000001.SZ"}
    assert result["600000.SH"]["count"] == 1 and result["600000.SH"]["mean_change_pct"] == pytest.approx(.01)
    assert result["000001.SZ"]["member_count"] == 0
    assert context.evaluate_sector_peers(quotes, members, []) == {}


def test_industry_members_resolve_codes_through_repository(monkeypatch):
    mapping = pl.DataFrame({"_sym_up": ["600000", "600001.SH"],
                            "industry": ["金融-银行-股份行", "制造-机械-工具"]})
    monkeypatch.setattr(context, "_load_concept_map_df", lambda *_: (mapping, 1))
    repo = SimpleNamespace(get_instruments=lambda: pl.DataFrame({"symbol": ["600000.SH"]}))
    result = context.load_sector_members(repo)
    assert result["600000.SH"] == "金融-银行"
    assert result["600001.SH"] == "制造-机械"


def test_industry_source_failure_is_isolated(monkeypatch):
    def broken(*_):
        raise RuntimeError("provider unavailable")
    monkeypatch.setattr(context, "_load_concept_map_df", broken)
    assert context.load_sector_members(SimpleNamespace()) == {}


@pytest.mark.parametrize("fresh_count,confirmed", [(3, False), (79, False), (80, True), (100, True)])
def test_sector_confirmation_requires_complete_member_coverage(fresh_count, confirmed):
    members = {f"{600000 + i}.SH": "制造-机械" for i in range(100)} | {CANDIDATE: "制造-机械"}
    symbols = [*list(members)[:fresh_count], CANDIDATE]
    quotes = pl.DataFrame({"symbol": symbols, "change_pct": [.06] * len(symbols)})
    result = peers_of(quotes, members)
    assert result["confirmed"] is confirmed
    assert result["member_count"] == 100
    assert result["count"] == fresh_count
    assert result["coverage"] == pytest.approx(fresh_count / 100)
    if not confirmed:
        assert "覆盖率" in result["reason"]


def test_sector_missing_invalid_and_duplicate_quotes_do_not_shrink_denominator():
    members = {f"{600000 + i}.SH": "制造-机械" for i in range(5)} | {CANDIDATE: "制造-机械"}
    quotes = pl.DataFrame({
        "symbol": [*list(members), "600000.SH", "600099.SH"],
        "change_pct": [.06, .06, .06, float("nan"), None, .09, .07, .1],
    })
    result = peers_of(quotes, members)
    assert result["member_count"] == 5
    assert result["count"] == 2
    assert result["coverage"] == .4
    assert result["confirmed"] is False
    empty = peers_of(pl.DataFrame(), members)
    assert empty["member_count"] == 5 and empty["coverage"] == 0
    assert empty["count"] == 0 and empty["mean_change_pct"] is None and "覆盖率" in empty["reason"]


def test_industry_aliases_are_not_counted_twice_in_coverage(monkeypatch):
    symbols = ["600000.SH", "600001.SH", "600002.SH"]
    mapping = pl.DataFrame({
        "_sym_up": [*symbols, "600000", "600001", "600002"],
        "industry": ["金融-银行"] * 6,
    })
    monkeypatch.setattr(context, "_load_concept_map_df", lambda *_: (mapping, 1))
    repo = SimpleNamespace(get_instruments=lambda: pl.DataFrame({"symbol": symbols}))
    members = context.load_sector_members(repo)
    assert members == dict.fromkeys(symbols, "金融-银行")
    result = peers_of(pl.DataFrame({"symbol": symbols, "change_pct": [.06, .06, .06]}), members,
                      candidate=symbols[0])
    # 别名若被重复计入, 同行分母会大于剔除本股后的 2 只。
    assert result["member_count"] == 2 and result["coverage"] == 1


@pytest.mark.parametrize("ambiguous", [False, True])
def test_unresolved_industry_member_cannot_silently_reduce_coverage(monkeypatch, ambiguous):
    mapping = pl.DataFrame({
        "_sym_up": ["600000.SH", "123456", "600001.SH"],
        "industry": ["金融-银行", "金融-银行", "制造-机械"],
    })
    monkeypatch.setattr(context, "_load_concept_map_df", lambda *_: (mapping, 1))
    symbols = ["600000.SH", "600001.SH"]
    if ambiguous:
        symbols += ["123456.SH", "123456.SZ"]
    repo = SimpleNamespace(get_instruments=lambda: pl.DataFrame({"symbol": symbols}))
    assert context.load_sector_members(repo) == {"600001.SH": "制造-机械"}


def test_unresolved_secondary_industry_does_not_shrink_primary_denominator(monkeypatch):
    mapping = pl.DataFrame({
        "_sym_up": ["600000.SH", "600000.SH", "123456", "600001.SH", "600002.SH", "600003.SH"],
        "industry": ["A-clean", "Z-invalid", "Z-invalid", "A-clean", "A-clean", "A-clean"],
    })
    monkeypatch.setattr(context, "_load_concept_map_df", lambda *_: (mapping, 1))
    symbols = [f"{600000 + i}.SH" for i in range(4)]
    repo = SimpleNamespace(get_instruments=lambda: pl.DataFrame({"symbol": symbols}))
    members = context.load_sector_members(repo)
    result = peers_of(pl.DataFrame({"symbol": symbols[1:], "change_pct": [.06] * 3}), members,
                      candidate=symbols[1])
    # 600000.SH 仍留在同行分母内, 无行情时不能被丢弃以抬高覆盖率。
    assert result["member_count"] == 3
    assert result["coverage"] == pytest.approx(2 / 3)
    assert result["confirmed"] is False
