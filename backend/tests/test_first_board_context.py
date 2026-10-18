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
