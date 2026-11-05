from datetime import date, datetime, time
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services import huichun_coverage, kline_sync

DAY = date(2026, 11, 5)
PREVIOUS = date(2026, 11, 4)
NOW = datetime.combine(DAY, time(16), CN_TZ)


def bars(symbols, dates):
    return pl.DataFrame({
        "symbol": symbols, "date": dates, "open": 10., "high": 11., "low": 9.,
        "close": 10., "volume": 100., "amount": 1000.,
    })


def test_only_requested_dated_official_batch_rows_receive_evidence():
    symbols = ["600001.SH", "600002.SH", "600003.SH", "600004.SH"]
    data = bars([*symbols, "outside", "600001.SH"], [DAY, DAY, DAY, None, DAY, PREVIOUS])
    data = data.with_columns(
        pl.when(pl.col("symbol") == symbols[1]).then(0.).otherwise(pl.col("high")).alias("high"),
        pl.when(pl.col("symbol") == symbols[2]).then(1).otherwise(None).alias("quote_ts"),
    )
    got = kline_sync._stamp_daily_batch(data, symbols, DAY, DAY, NOW)
    assert got.filter(pl.col("daily_batch_finalized"))["symbol"].to_list() == symbols[:1]
    assert got["symbol"].to_list() == symbols[:1]  # Invalid replies cannot upsert over valid local bars.
    assert set(got["symbol"]) <= set(symbols)
    assert set(got["date"]) == {DAY}
    early = kline_sync._stamp_daily_batch(data, symbols, DAY, DAY, NOW.replace(hour=14))
    assert not early["daily_batch_finalized"].any()


def test_preflight_uses_per_symbol_proof_and_targets_missing_active_symbols():
    today = bars(["A", "B", "C", "C"], [DAY] * 4).with_columns(
        pl.Series("daily_batch_finalized", [True, False, True, True]),
        pl.Series("quote_ts", [None, int(NOW.timestamp() * 1000), None, None], dtype=pl.Int64),
    )
    rows = pl.concat([today, bars(["A", "B", "C", "D"], [PREVIOUS] * 4)], how="diagonal_relaxed")
    counts, needs = huichun_coverage.daily_check(rows, DAY, PREVIOUS, NOW)
    assert counts["finalized_symbols"] == 1
    assert counts["unfinalized_symbols"] == 2
    assert needs == ["B", "C", "D"]
    legacy, codes = huichun_coverage.daily_check(rows.drop("daily_batch_finalized"), DAY, PREVIOUS, NOW)
    assert legacy["finalized_symbols"] == 0
    assert codes == ["A", "B", "C", "D"]


def test_recheck_rejects_unclosed_or_non_market_date_before_contacting_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(kline_sync, "cn_now", lambda: NOW.replace(hour=15, minute=20))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    for day in (DAY, date(2026, 11, 1)):
        with pytest.raises(ValueError):
            kline_sync.confirm_daily_session(repo, object(), day, ["600001.SH"])


def test_recheck_missing_ohlc_or_empty_response_leaves_no_proof(tmp_path, monkeypatch):
    monkeypatch.setattr(kline_sync, "cn_now", lambda: NOW)
    monkeypatch.setattr(kline_sync.preferences, "get_daily_data_provider", lambda: "tickflow")
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    for frame in (pl.DataFrame(), bars(["600001.SH"], [DAY]).drop("amount")):
        monkeypatch.setattr(kline_sync, "iter_daily_frames", lambda *a, sample=frame, **kw: iter([sample]))
        assert kline_sync.confirm_daily_session(repo, object(), DAY, ["600001.SH"]) == 0


def test_partial_recheck_replaces_only_verified_rows_and_recomputes(tmp_path, monkeypatch):
    from app.services import daily_health
    from app.tickflow.repository import DataStore, KlineRepository

    store = DataStore(tmp_path)
    repo = KlineRepository(store)
    syms = ["600001.SH", "600002.SH", "600003.SH"]
    repo.append_daily(pl.concat([bars(syms, [PREVIOUS] * 3), bars(syms, [DAY] * 3)]))
    store._register_views()
    monkeypatch.setattr(kline_sync, "cn_now", lambda: NOW)
    monkeypatch.setattr(kline_sync.preferences, "get_daily_data_provider", lambda: "tickflow")
    def fetch(symbols, capset, **kwargs):
        assert symbols == syms and kwargs["start_time"].date() == kwargs["end_time"].date() == DAY
        yield bars(syms[:2], [DAY, PREVIOUS]).with_columns(pl.lit(10.5).alias("close"))
        yield bars([syms[1]], [DAY]).drop("date")
        yield bars([syms[2], syms[2]], [DAY, DAY])
    monkeypatch.setattr(kline_sync, "iter_daily_frames", fetch)
    try:
        assert kline_sync.confirm_daily_session(repo, object(), DAY, syms) == 1
        rows = pl.read_parquet(tmp_path / f"kline_daily/date={DAY}/part.parquet")
        assert rows.filter(pl.col("daily_batch_finalized"))["symbol"].to_list() == syms[:1]
        enr = pl.read_parquet(tmp_path / f"kline_daily_enriched/date={DAY}/part.parquet")
        assert enr.select("symbol", "raw_close").rows() == [(syms[0], 10.5)]
        assert daily_health.snapshot(tmp_path)["entries"] == []
    finally:
        store.db.close()


def test_recheck_pending_cache_refresh_is_retried(tmp_path):
    from app.services import daily_health
    from app.services.daily_confirmation import PENDING_REFRESH
    from app.services.data_integrity import IntegrityIssue

    codes = ["600001.SH"]
    daily_health.record_issues(tmp_path, [IntegrityIssue(DAY, "kline_daily", "snapshot",
                               symbols=tuple(codes), reason=PENDING_REFRESH)])
    daily_health.mark_enriched_ready(tmp_path, "stock", start=DAY, symbols=codes, days={DAY})
    assert huichun_coverage.known_raw_issues(tmp_path, DAY) == (True, codes)
    daily_health.complete(tmp_path, "stock", start=DAY, symbols=codes, days={DAY})
    assert huichun_coverage.known_raw_issues(tmp_path, DAY) == (False, [])


def test_corrected_volume_or_high_rebuilds_existing_enriched_without_a_close_change(tmp_path):
    from app.jobs.daily_pipeline import _prune_stale_price_partitions

    raw = bars(["600001.SH"], [DAY])
    daily = tmp_path / "daily"
    enriched = tmp_path / "enriched"
    for root, frame in (
        (daily, raw),
        (enriched, raw.with_columns(
            pl.col("close").alias("raw_close"), pl.col("high").alias("raw_high"),
            pl.col("low").alias("raw_low"), pl.lit(1.).alias("volume"),
        )),
    ):
        part = root / f"date={DAY}"
        part.mkdir(parents=True)
        frame.write_parquet(part / "part.parquet")
    assert _prune_stale_price_partitions(daily, enriched) == [str(DAY)]


def test_identical_final_values_clear_live_enriched_provenance(tmp_path):
    from app.jobs.daily_pipeline import _prune_stale_price_partitions

    raw = bars(["600001.SH"], [DAY]).with_columns(pl.lit(True).alias("daily_batch_finalized"))
    daily = tmp_path / "daily" / f"date={DAY}"
    enriched = tmp_path / "enriched" / f"date={DAY}"
    daily.mkdir(parents=True)
    enriched.mkdir(parents=True)
    raw.write_parquet(daily / "part.parquet")
    raw.with_columns(pl.col("close").alias("raw_close"), pl.col("high").alias("raw_high"),
                     pl.col("low").alias("raw_low"), pl.lit(1, dtype=pl.Int64).alias("quote_ts")).write_parquet(
        enriched / "part.parquet"
    )
    assert _prune_stale_price_partitions(daily.parent, enriched.parent) == [str(DAY)]


def test_conflicts_across_stream_chunks_never_overwrite_or_certify(tmp_path, monkeypatch):
    from app.data_providers import custom
    from app.tickflow.repository import DataStore, KlineRepository

    syms = ["600001.SH", "600002.SH"]
    frames = [
        bars(syms, [DAY, DAY]),
        bars([syms[0], syms[0]], [DAY, DAY]).with_columns(pl.lit(0.).alias("volume")),
    ]
    provider = SimpleNamespace(iter_daily=lambda *a, **kw: iter(frames))
    monkeypatch.setattr(kline_sync, "cn_now", lambda: NOW)
    monkeypatch.setattr(kline_sync.preferences, "get_daily_data_provider", lambda: "custom")
    monkeypatch.setattr(custom, "provider_has_dataset", lambda *args: True)
    monkeypatch.setattr(custom, "get_provider", lambda *args: provider)
    repo = KlineRepository(DataStore(tmp_path))
    try:
        for existing in (False, True):
            if existing:
                repo.append_daily(bars(syms[:1], [DAY]).with_columns(pl.lit(9.5).alias("close")))
            zero = []
            assert kline_sync.sync_and_persist_daily_batch(
                syms, repo, object(), start_date=NOW, end_date=NOW, zero_row_out=zero,
            ) == 1
            assert zero == syms[:1]
            assert repo.execute_all(
                "SELECT symbol FROM kline_daily WHERE daily_batch_finalized ORDER BY symbol"
            ) == [(syms[1],)]
            frame = pl.concat([pl.read_parquet(path) for path in
                               (tmp_path / f"kline_daily/date={DAY}").glob("*.parquet")], how="diagonal_relaxed")
            assert frame.filter(pl.col("daily_batch_finalized"))["symbol"].to_list() == syms[1:]
            assert frame.filter(pl.col("symbol") == syms[0])["close"].to_list() == ([9.5] if existing else [])
    finally:
        repo.store.db.close()
