"""Shared A0 inputs remain read-only and respect the historical cutoff."""

import json
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.services.huichun_audit import FACTOR_SCHEMA
from app.services.huichun_data import HuichunDataReader


def _repo(root, days=()):
    return SimpleNamespace(
        store=SimpleNamespace(data_dir=root),
        execute_one=lambda *_: (1,),
        execute_all=lambda _, args: [(day,) for day in days if day <= args[0]],
    )


def _calendar(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 1, "coverage_start": "2026-10-12", "coverage_end": "2026-10-16",
        "weekends_closed": True, "closures": [], "sources": [{
            "id": "test", "url": "https://www.sse.com.cn/notice",
            "published_on": "2025-12-01", "title": "Synthetic test calendar",
        }],
    }), encoding="utf-8")


def test_reader_does_not_initialize_or_repair_mode_state(tmp_path):
    root = tmp_path / "data"
    config = root / "user_data/huichun/config.json"
    config.parent.mkdir(parents=True)
    config.write_text("corrupt mode state", encoding="utf-8")
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    reader = HuichunDataReader(_repo(root))
    assert reader.calendar_path.is_file()
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_calendar_prefers_user_file_and_includes_missing_sessions(tmp_path):
    path = tmp_path / "user_data/huichun/market_calendar.json"
    _calendar(path)
    reader = HuichunDataReader(_repo(tmp_path, [date(2026, 10, 12), date(2026, 10, 14)]))
    assert reader.calendar_path == path
    days, source = reader.calendar(date(2026, 10, 14))
    assert days == [date(2026, 10, day) for day in (12, 13, 14)]
    assert source == "official_exchange_calendar"


@pytest.mark.parametrize("path_kind", ["disabled", "missing", "outside"])
def test_unavailable_calendar_retains_observed_dates(tmp_path, path_kind):
    path = tmp_path / "calendar.json"
    if path_kind == "outside":
        _calendar(path)
    days = [date(2026, 10, 12), date(2026, 10, 19)]
    reader = HuichunDataReader(_repo(tmp_path, days), calendar_path=None if path_kind == "disabled" else path)
    assert reader.calendar(days[-1]) == (days, "observed_market_daily_dates")


def test_signature_ignores_only_proven_future_daily_partitions(tmp_path):
    current = tmp_path / "kline_daily/date=2026-10-12/part.parquet"
    future = tmp_path / "kline_daily/date=2026-10-13/part.parquet"
    factor = tmp_path / "adj_factor/part.parquet"
    for path in (current, future, factor):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"initial")
    reader = HuichunDataReader(_repo(tmp_path), calendar_path=None)
    before = reader.input_signature(date(2026, 10, 12))
    future.write_bytes(b"future change")
    assert reader.input_signature(date(2026, 10, 12)) == before
    factor.write_bytes(b"factor change")
    assert reader.input_signature(date(2026, 10, 12)) != before


def test_prepare_quarantines_dated_factor_without_touching_mode_state():
    days = [date(2026, 10, day) for day in (12, 13, 14)]
    raw = pl.DataFrame({
        "symbol": ["600001.SH"] * 3, "date": days,
        "open": [10.] * 3, "high": [10.] * 3, "low": [10.] * 3,
        "close": [10.] * 3, "volume": [100.] * 3, "amount": [1000.] * 3,
    })
    factors = pl.DataFrame({"symbol": ["600001.SH"], "trade_date": [days[-1]], "ex_factor": [-1.]}, schema=FACTOR_SCHEMA)
    prepared, issues = HuichunDataReader.prepare_batch(
        raw, factors, calendar=days, end=days[-1], listings={},
        expected_symbols=["600001.SH"], calendar_verified=True,
    )
    assert issues == {"600001.SH": days[-1]}
    assert prepared.frame["date"].to_list() == days[:2]
    assert prepared.frame["close"].to_list() == [10., 10.]
