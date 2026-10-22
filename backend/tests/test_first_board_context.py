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


def test_sector_confirmation_uses_fraction_units_and_unique_quotes():
    members = {"600000.SH": "金融-银行", "600001.SH": "金融-银行", "600002.SH": "金融-银行"}
    quotes = pl.DataFrame({"symbol": list(members), "change_pct": [.06, .05, -.01]})
    result = context.evaluate_sectors(quotes, members)["金融-银行"]
    assert result["confirmed"] and result["count"] == 3
    assert result["mean_change_pct"] == pytest.approx(1 / 30)
    duplicated = pl.concat([quotes, quotes.head(1)])
    result = context.evaluate_sectors(duplicated, members)["金融-银行"]
    assert not result["confirmed"] and result["count"] == 2


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
    members = {f"{600000 + i}.SH": "制造-机械" for i in range(100)}
    quotes = pl.DataFrame({"symbol": list(members)[:fresh_count], "change_pct": [.06] * fresh_count})
    result = context.evaluate_sectors(quotes, members)["制造-机械"]
    assert result["confirmed"] is confirmed
    assert result["member_count"] == 100
    assert result["count"] == fresh_count
    assert result["coverage"] == pytest.approx(fresh_count / 100)
    if not confirmed:
        assert "覆盖率" in result["reason"]


def test_sector_missing_invalid_and_duplicate_quotes_do_not_shrink_denominator():
    members = {f"{600000 + i}.SH": "制造-机械" for i in range(5)}
    quotes = pl.DataFrame({
        "symbol": [*list(members), "600000.SH", "600099.SH"],
        "change_pct": [.06, .06, .06, float("nan"), None, .07, .1],
    })
    result = context.evaluate_sectors(quotes, members)["制造-机械"]
    assert result["member_count"] == 5
    assert result["count"] == 2
    assert result["coverage"] == .4
    assert result["confirmed"] is False
    empty = context.evaluate_sectors(pl.DataFrame(), members)["制造-机械"]
    assert empty["member_count"] == 5 and empty["coverage"] == 0


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
    result = context.evaluate_sectors(
        pl.DataFrame({"symbol": symbols, "change_pct": [.06, .06, .06]}), members,
    )["金融-银行"]
    assert result["member_count"] == 3 and result["coverage"] == 1
    assert result["confirmed"] is True


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
    result = context.evaluate_sectors(
        pl.DataFrame({"symbol": symbols[1:], "change_pct": [.06] * 3}), members,
    )["A-clean"]
    assert result["member_count"] == 4
    assert result["coverage"] == .75
    assert result["confirmed"] is False
