"""Follow-up observations must work with normal post-close batch rows (null quote_ts)."""
from datetime import date, datetime

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.index_const import CORE_INDEX_SYMBOLS
from app.services.market_game_evaluation import evaluate_observations

INDEX = CORE_INDEX_SYMBOLS[0]
DAYS = [date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11), date(2026, 11, 12)]


def _stamp(day, hour):
    return datetime(day.year, day.month, day.day, hour, 5, tzinfo=CN_TZ).timestamp() * 1000


class Repo:
    def __init__(self, stock_ts, index_ts):
        closes = [10.0, 10.5, 10.2, 11.0]
        self.daily = [("600001.SH", d, c, stock_ts[i], c * 1.02, c * 0.98) for i, (d, c) in enumerate(zip(DAYS, closes, strict=True))]
        self.index = [(INDEX, d, 100.0 + i, index_ts[i], None, None) for i, d in enumerate(DAYS)]

    def execute_all(self, sql, params=None):
        if sql.startswith("DESCRIBE"):
            return [(name,) for name in ("symbol", "date", "close", "quote_ts", "high", "low")]
        return self.index if "kline_index_daily" in sql else self.daily

    def get_index_daily(self, symbol, start, end, columns=None):
        return pl.DataFrame({"date": DAYS, "close": [row[2] for row in self.index]})

    def get_daily_batch(self, symbols, start, end, columns=None):
        return pl.DataFrame({"symbol": [r[0] for r in self.daily], "date": DAYS,
                             "close": [r[2] for r in self.daily], "high": [r[4] for r in self.daily],
                             "low": [r[5] for r in self.daily], "raw_close": [r[2] for r in self.daily]})


REPORT = {
    "id": "mg_" + "0" * 32, "as_of": "2026-11-09", "cutoff": "2026-11-09T21:45:00+08:00",
    "candidates": [{"symbol": "600001.SH", "name": "样本"}],
    "validity": {"calendar_verified": True, "status": "scheduled", "entry_session": "2026-11-10",
                 "observation_sessions": ["2026-11-10", "2026-11-11", "2026-11-12"],
                 "expires_at": "2026-11-12T15:00:00+08:00"},
}
NOW = datetime(2026, 11, 12, 18, tzinfo=CN_TZ)


def test_batch_rows_without_timestamps_are_shown_as_unverified_closes():
    result = evaluate_observations(Repo([None] * 4, [None] * 4), REPORT, now=NOW)
    rows = result["rows"]
    assert [row["state"] for row in rows] == ["unverified"] * 3
    assert rows[0]["close_return"] == pytest.approx(0.05)
    assert rows[2]["benchmark_return"] == pytest.approx(0.03)
    assert rows[2]["max_upside"] == pytest.approx(11.0 * 1.02 / 10 - 1)
    assert result["status"] == "complete"
    assert any("收盘未核验" in text for text in result["limitations"])


def test_certified_closes_stay_available_and_intraday_stamps_are_rejected():
    certified = [_stamp(d, 15) for d in DAYS]
    result = evaluate_observations(Repo(certified, certified), REPORT, now=NOW)
    assert [row["state"] for row in result["rows"]] == ["available"] * 3
    intraday = [certified[0], _stamp(DAYS[1], 14), certified[2], certified[3]]
    rows = evaluate_observations(Repo(intraday, certified), REPORT, now=NOW)["rows"]
    assert rows[0]["state"] == "unavailable" and rows[0]["close_return"] is None
    assert rows[1]["state"] == "available"


def test_sessions_after_the_evaluation_close_remain_pending():
    early = datetime(2026, 11, 11, 14, 40, tzinfo=CN_TZ)
    rows = evaluate_observations(Repo([None] * 4, [None] * 4), REPORT, now=early)["rows"]
    assert [row["state"] for row in rows] == ["unverified", "pending", "pending"]
