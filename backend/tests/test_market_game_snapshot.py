"""Snapshot coverage and partial historical price-limit evidence."""
from datetime import date, datetime, timedelta

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services import market_game_snapshot as snapshot
from app.tickflow.repository import DataStore, KlineRepository


def _metadata(symbols, metadata_day=date(2026, 11, 1)):
    return pl.DataFrame({
        "symbol": symbols, "name": ["样本股"] * len(symbols),
        "listing_date": [date(2010, 1, 1)] * len(symbols),
        "metadata_date": [metadata_day] * len(symbols), "status": ["active"] * len(symbols),
    })


def _prices(symbols, days, closes):
    return pl.DataFrame([
        {
            "symbol": symbol, "date": day, "open": close,
            "high": close + .01, "low": close - .01, "close": close,
            "raw_close": close, "raw_high": close + .01, "raw_low": close - .01,
            "volume": 1_000., "amount": 100_000_000.,
        }
        for symbol in symbols for day, close in zip(days, closes, strict=True)
    ])


def test_coverage_uses_recent_sessions_and_never_future_symbols():
    days = [date(2026, 9, 1) + timedelta(days=i * 3) for i in range(23)]
    frame = pl.DataFrame({
        "symbol": ["active"] * 23 + ["inactive", "recent_missing", "future"],
        "date": [*days, days[0], days[-2], days[-1] + timedelta(days=1)],
    })
    result = snapshot._coverage_denominators(frame, days)
    assert result[days[0]] == 2
    assert result[days[19]] == 2
    assert result[days[20]] == 1  # expires after 20 sessions, not 20 natural days
    assert result[days[-1]] == 2  # missing current day stays in the denominator
    assert snapshot._coverage_denominators(pl.DataFrame(), days) == dict.fromkeys(days, 0)


def test_snapshot_excludes_metadata_only_delisted_but_counts_raw_only_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot, "cn_now", lambda: datetime(2026, 10, 9, 16, tzinfo=CN_TZ))
    store = DataStore(tmp_path)
    repo = KlineRepository(store)
    days = [date(2026, 9, d) for d in (28, 29, 30)]
    records = _prices(["600000.SH"], days, [10., 10.1, 10.2])
    repo.append_enriched(records)
    raw_only = _prices(["000002.SZ"], days[:-1], [10., 10.1])
    pl.concat([records, raw_only]).drop("raw_close", "raw_high", "raw_low").write_parquet(
        tmp_path / "kline_daily" / "sample.parquet",
    )
    _metadata(["600000.SH", "000002.SZ", "600001.SH", "600002.SH"]).rename({
        "metadata_date": "as_of",
    }).write_parquet(tmp_path / "instruments" / "sample.parquet")
    store._register_views()
    try:
        result = snapshot.build_snapshot(repo, days[-1])
        assert result["coverage"] == .5
        assert "coverage_insufficient" in result["quality_reasons"]
        assert result["history"][-1]["coverage"] == .5
    finally:
        store.db.close()


@pytest.mark.parametrize("symbol,day,close,known", [
    ("300001.SZ", date(2025, 9, 9), 12., True),
    ("688001.SH", date(2025, 9, 9), 12., True),
    ("920001.BJ", date(2025, 9, 9), 13., True),
    ("600001.SH", date(2025, 9, 9), 11., False),
    ("300001.SZ", date(2020, 8, 21), 11., False),
    ("600001.SH", date(2026, 7, 6), 11., True),
])
def test_only_state_dependent_unknown_price_limits_are_excluded(symbol, day, close, known):
    days = [day - timedelta(days=1), day]
    prepared = snapshot._prepare(_prices([symbol], days, [10., close]), days,
                                 _metadata([symbol]), datetime(2026, 11, 1, 16, tzinfo=CN_TZ))
    assert prepared["signal_limit_up"].to_list() == [None, True if known else None]


def test_partial_limit_statistics_keep_known_samples_and_disclose_denominator():
    days = [date(2025, 9, d) for d in (8, 9, 10)]
    symbols = ["300001.SZ", "600001.SH"]
    prepared = snapshot._prepare(_prices(symbols, days, [10., 12., 14.4]), days,
                                 _metadata(symbols), datetime(2026, 11, 1, 16, tzinfo=CN_TZ))
    history = snapshot._aggregate(prepared, {}, days, dict.fromkeys(days, 2))
    assert history[0]["limit_up"] is None
    assert history[0]["seal_rate"] is None
    assert history[0]["max_boards"] is None
    assert history[-1]["limit_sample_count"] == 1
    assert history[-1]["limit_coverage"] == .5
    assert history[-1]["limit_up"] == 1
    assert history[-1]["limit_down"] == 0
    assert history[-1]["limit_up_ratio"] == 1
    assert history[-1]["seal_rate"] == 1
    assert history[-1]["max_boards"] == 2
    assert history[-1]["previous_limit_premium"] == pytest.approx(.2)


def test_dated_main_board_metadata_can_supply_historical_limit_statistics():
    days = [date(2025, 9, d) for d in (8, 9)]
    instruments = _metadata(["600001.SH"], days[0]).with_columns(pl.lit("ST样本").alias("name"))
    result = snapshot._prepare(_prices(["600001.SH"], days, [10., 10.5]), days,
                               instruments, datetime(2025, 9, 9, 16, tzinfo=CN_TZ))
    assert result["signal_limit_up"].to_list() == [None, True]


def test_snapshot_freezes_mainline_config_and_complete_membership(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot, "cn_now", lambda: datetime(2026, 9, 30, 16, tzinfo=CN_TZ))
    config = {"min_members": 4, "max_members": 600, "blacklist": ["宽基", "泛概念"], "exclude_st": True}
    monkeypatch.setattr("app.services.preferences.get_mainline_filter_config", lambda: dict(config))
    monkeypatch.setattr(snapshot, "_current_sectors", lambda repo: {
        "600000.SH": ["主题"], "600000": ["主题"], "000002.SZ": ["主题"],
    })
    store = DataStore(tmp_path)
    repo = KlineRepository(store)
    days = [date(2026, 9, d) for d in (28, 29, 30)]
    records = _prices(["600000.SH"], days, [10., 11., 12.1])
    repo.append_enriched(records)
    records.drop("raw_close", "raw_high", "raw_low").write_parquet(tmp_path / "kline_daily" / "sample.parquet")
    _metadata(["600000.SH"]).rename({"metadata_date": "as_of"}).write_parquet(
        tmp_path / "instruments" / "sample.parquet",
    )
    store._register_views()
    try:
        first = snapshot.build_snapshot(repo)
        assert first["sector_member_counts"] == {"主题": 2}
        assert first["stocks"][0]["boards"] == 2
        assert first["mainline_filter"] == config
        config["max_members"] = 100
        second = snapshot.build_snapshot(repo)
        assert second["input_version"] != first["input_version"]
        assert first["mainline_filter"]["max_members"] == 600
    finally:
        store.db.close()
