"""Contract checks against the refactored main data and signal architecture."""
from datetime import date, datetime

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.huichun_calendar import BUNDLED_CALENDAR_PATH
from app.services.market_game_calendar import _calendar_paths, load_game_calendar
from app.services.market_game_snapshot import _instruments, _prepare


def _prices(closes, *, scale=1.0):
    days = [date(2026, 10, 8 + index) for index in range(len(closes))]
    rows = [{
        "symbol": "600000.SH", "date": day,
        "open": price * scale, "high": (price + .1) * scale,
        "low": (price - .1) * scale, "close": price * scale,
        "raw_close": price, "raw_high": price + .1, "raw_low": price - .1,
        "volume": 1000., "amount": 1_000_000.,
    } for day, price in zip(days, closes, strict=True)]
    return pl.DataFrame(rows), days


def _metadata():
    class Repo:
        def get_instruments(self):
            return pl.DataFrame({
                "symbol": ["600000.SH"], "name": ["浦发银行"],
                "listing_date": [date(1999, 11, 10)], "as_of": [date(2026, 10, 8)],
            })

    return _instruments(Repo())


@pytest.mark.parametrize("scale", [1.0, .5])
def test_price_states_do_not_depend_on_custom_signal_definitions(monkeypatch, scale):
    monkeypatch.setattr("app.indicators.pipeline._get_custom_signal_exprs", lambda: {})
    frame, days = _prices([10., 11., 9.9], scale=scale)
    result = _prepare(frame, days, _metadata(), datetime(2026, 10, 10, 16, tzinfo=CN_TZ))
    assert result["signal_limit_up"].to_list() == [None, True, False]
    assert result["signal_limit_down"].to_list() == [None, False, True]
    assert result["boards"].to_list() == [0, 1, 0]
    assert result["change_pct"].to_list()[1:] == pytest.approx([.1, -.1])


def test_custom_signal_redefinition_cannot_change_market_price_statistics(monkeypatch):
    monkeypatch.setattr("app.indicators.pipeline._get_custom_signal_exprs", lambda: {
        "signal_limit_up": pl.col("raw_close") < pl.col("limit_up_price"),
    })
    frame, days = _prices([10., 11.])
    result = _prepare(frame, days, _metadata(), datetime(2026, 10, 9, 16, tzinfo=CN_TZ))
    assert result["signal_limit_up"].to_list() == [None, True]


def test_broken_board_uses_raw_high_and_exchange_tolerance(monkeypatch):
    monkeypatch.setattr("app.indicators.pipeline._get_custom_signal_exprs", lambda: {})
    frame, days = _prices([10., 10.5], scale=.5)
    frame = frame.with_columns(
        pl.when(pl.col("date") == days[-1]).then(11.).otherwise(pl.col("raw_high")).alias("raw_high"),
        pl.when(pl.col("date") == days[-1]).then(5.5).otherwise(pl.col("high")).alias("high"),
    )
    result = _prepare(frame, days, _metadata(), datetime(2026, 10, 9, 16, tzinfo=CN_TZ))
    assert result["signal_broken_limit_up"].to_list() == [None, True]
    assert result["signal_limit_up"].to_list() == [None, False]


def test_calendar_uses_packaged_resource_and_keeps_user_override_priority(tmp_path):
    assert _calendar_paths(tmp_path)[-1] == BUNDLED_CALENDAR_PATH
    result = load_game_calendar(
        tmp_path, start=date(2026, 9, 1), cutoff=datetime(2026, 9, 30, 16, tzinfo=CN_TZ),
    )
    assert result["verified"] is True
    assert result["entry_session"] == "2026-10-08"
    assert result["exit_sessions"] == ["2026-10-08", "2026-10-09", "2026-10-12"]
    custom = _calendar_paths(tmp_path)[0]
    custom.parent.mkdir(parents=True)
    custom.write_text("invalid", encoding="utf-8")
    invalid = load_game_calendar(
        tmp_path, start=date(2026, 9, 1), cutoff=datetime(2026, 9, 30, 16, tzinfo=CN_TZ),
    )
    assert invalid["state"] == "invalid"
    assert invalid["verified"] is False
