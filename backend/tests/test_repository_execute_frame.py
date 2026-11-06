"""Columnar reads preserve query values and the repository's concurrency boundary."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import duckdb
import polars as pl
import pytest
from polars.testing import assert_frame_equal

from app.services.huichun_mode import HuichunModeService
from app.tickflow.repository import KlineRepository


@pytest.fixture
def repo():
    db = duckdb.connect()
    repository = KlineRepository(SimpleNamespace(db=db, data_dir=Path(".")))
    try:
        yield repository
    finally:
        db.close()


def test_frame_preserves_nulls_dates_timestamps_booleans_and_empty_schema(repo):
    query = """
        SELECT ?::DATE AS day, ?::TIMESTAMP AS stamp, ?::DOUBLE AS price,
               ?::BIGINT AS volume, ?::BOOLEAN AS finalized, ?::VARCHAR AS symbol
        UNION ALL SELECT NULL, NULL, NULL, NULL, NULL, NULL
    """
    params = [date(2026, 11, 5), datetime(2026, 11, 5, 15, 30), 10.5, 123, True, "600001.SH"]
    expected = pl.DataFrame(
        repo.execute_all(query, params),
        schema={"day": pl.Date, "stamp": pl.Datetime("us"), "price": pl.Float64,
                "volume": pl.Int64, "finalized": pl.Boolean, "symbol": pl.String},
        orient="row",
    )
    assert_frame_equal(repo.execute_frame(query, params), expected)
    assert_frame_equal(repo.execute_frame(f"SELECT * FROM ({query}) LIMIT 0", params), expected.head(0))


@pytest.mark.parametrize("failure", ["execute", "pl"])
def test_failed_columnar_read_releases_cursor_and_read_slot(repo, monkeypatch, failure):
    cursor = Mock()
    cursor.execute.return_value = cursor
    getattr(cursor, failure).side_effect = ValueError("query or conversion failed")
    monkeypatch.setattr(repo, "_new_cursor", lambda: cursor)
    repo._read_limiter = threading.Semaphore(1)
    with pytest.raises(ValueError, match="query or conversion failed"):
        repo.execute_frame("SELECT 1")
    cursor.close.assert_called_once()
    assert repo._read_limiter.acquire(blocking=False)
    repo._read_limiter.release()


@pytest.mark.parametrize("capacity", [1, 3])
def test_columnar_and_tuple_queries_share_read_capacity(repo, monkeypatch, capacity):
    repo._read_limiter = threading.Semaphore(capacity)
    active, maximum = 0, 0
    lock = threading.Lock()

    class Cursor:
        def execute(self, *_):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            return self

        def pl(self):
            time.sleep(0.04)
            return pl.DataFrame({"x": [1]})

        def fetchall(self):
            time.sleep(0.04)
            return [(1,)]

        def close(self):
            nonlocal active
            with lock:
                active -= 1

    monkeypatch.setattr(repo, "_new_cursor", Cursor)
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(getattr(repo, method), "SELECT 1")
                   for method in ("execute_frame", "execute_all") * 3]
        for future in futures:
            future.result(timeout=3)
    assert maximum == capacity
    assert active == 0


def test_huichun_raw_retains_legacy_schema_cutoffs_and_optional_nulls(repo):
    repo.db.execute("""
        CREATE TABLE kline_daily AS
        SELECT '600001.SH' AS symbol, DATE '2026-11-05' AS date,
               10::INTEGER AS open, 11::INTEGER AS high, 9::INTEGER AS low,
               10.5::DOUBLE AS close, 1000::BIGINT AS volume, 10500::BIGINT AS amount,
               NULL::BIGINT AS quote_ts, NULL::BOOLEAN AS daily_batch_finalized
        UNION ALL
        SELECT '600001.SH', DATE '2026-11-06', 11, 12, 10, 11.5, 2000, 23000,
               1793948400000, TRUE
    """)
    service = HuichunModeService.__new__(HuichunModeService)
    service.repo = repo
    expected_schema = {
        "symbol": pl.String, "date": pl.Date, "open": pl.Float64, "high": pl.Float64,
        "low": pl.Float64, "close": pl.Float64, "volume": pl.Float64,
        "amount": pl.Float64, "quote_ts": pl.Float64, "daily_batch_finalized": pl.Boolean,
    }
    expected = pl.DataFrame(repo.execute_all("SELECT * FROM kline_daily ORDER BY symbol,date"),
                            schema=expected_schema, orient="row")
    assert_frame_equal(service._raw(None, date(2026, 11, 6)), expected)
    assert_frame_equal(service._raw(["600001.SH"], date(2026, 11, 5)), expected.head(1))
    assert_frame_equal(service._raw(None, date(2026, 11, 6), start=date(2026, 11, 6)), expected.tail(1))
    assert_frame_equal(service._raw(["600002.SH"], date(2026, 11, 6)), expected.head(0))
