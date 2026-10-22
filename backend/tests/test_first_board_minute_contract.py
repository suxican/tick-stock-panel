"""New minute evidence must not certify unknown legacy rows or incomplete grids."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest

from app.data_providers.custom.provider import GenericHTTPProvider
from app.market_time import CN_TZ
from app.services import first_board_attention as attention
from app.services import first_board_attention_data as inputs
from app.services import kline_sync, minute_adjust
from app.tickflow.repository import DataStore, KlineRepository

SYMBOL = "000001.SZ"
DAY = datetime(2026, 10, 22)


def stamp(hour, minute):
    return DAY.replace(hour=hour, minute=minute)


def auction_bars(*, evidence=True):
    times = [stamp(9, 30) + timedelta(minutes=i) for i in range(121)]
    times += [stamp(13, 1) + timedelta(minutes=i) for i in range(120)]
    rows = []
    for i, dt in enumerate(times):
        price = 10 + i * .001
        rows.append({"symbol": SYMBOL, "datetime": dt, "open": price, "high": price,
                     "low": price, "close": price, "volume": 100., "amount": price * 10_000})
    frame = pl.DataFrame(rows)
    if evidence:
        frame = frame.with_columns(pl.lit("raw").alias("minute_price_basis"),
                                   pl.lit("auction_end").alias("minute_time_grid"))
    return frame


def intraday(frame, hour=13, minute=10):
    return attention._intraday(frame, {SYMBOL}, stamp(hour, minute).replace(tzinfo=CN_TZ), True, None)[SYMBOL]


def store(tmp_path, frame):
    kline_sync._write_minute_partition(frame, tmp_path / "kline_minute")
    return tmp_path / "kline_minute" / f"date={DAY.date()}" / "part.parquet"


def test_241_grid_keeps_opening_auction_in_day_vwap_without_crossing_lunch():
    frame = auction_bars()
    # A large, higher-priced opening auction makes close below the true day VWAP.
    frame = frame.with_columns([
        pl.when(pl.col("datetime") == stamp(9, 30)).then(value).otherwise(pl.col(col)).alias(col)
        for col, value in {"open": 11., "high": 11., "low": 11., "close": 11.,
                           "volume": 100_000., "amount": 110_000_000.}.items()
    ])
    result = intraday(frame)
    assert result["return_5m"] == pytest.approx(10.129 / 10.124 - 1)
    assert result["above_vwap"] is False
    assert result["as_of"] == stamp(13, 9).replace(tzinfo=CN_TZ).isoformat()
    assert intraday(frame, 13, 5)["return_5m"] is None


@pytest.mark.parametrize("problem", ["missing_1130", "missing_middle", "duplicate", "extra_1300",
                                    "missing_open", "mixed_grid", "unknown_basis", "forward_basis"])
def test_declared_auction_grid_still_rejects_gaps_mixed_labels_and_unknown_prices(problem):
    frame = auction_bars()
    if problem.startswith("missing"):
        removed = {"missing_1130": stamp(11, 30), "missing_middle": stamp(10, 20),
                   "missing_open": stamp(9, 30)}[problem]
        frame = frame.filter(pl.col("datetime") != removed)
    elif problem == "duplicate":
        frame = pl.concat([frame, frame.head(1)])
    elif problem == "extra_1300":
        frame = pl.concat([frame, frame.head(1).with_columns(pl.lit(stamp(13, 0)).alias("datetime"))])
    else:
        col, value = (("minute_time_grid", "start") if problem == "mixed_grid" else
                      ("minute_price_basis", None if problem == "unknown_basis" else "forward"))
        frame = frame.with_columns(pl.when(pl.col("datetime") == stamp(9, 31)).then(pl.lit(value))
                                   .otherwise(pl.col(col)).alias(col))
    assert intraday(frame)["return_5m"] is None


def test_undeclared_241_grid_is_not_guessed_and_unfinished_rows_never_enter_features():
    assert intraday(auction_bars(evidence=False))["return_5m"] is None
    frame = auction_bars()
    before = intraday(frame)
    future = frame.with_columns(pl.when(pl.col("datetime") >= stamp(13, 10)).then(-100.)
                                .otherwise(pl.col("close")).alias("close"))
    assert intraday(future) == before
    assert intraday(frame, 12, 0)["return_5m"] is None


def test_new_intraday_entry_points_attach_evidence_but_plain_normalization_does_not(monkeypatch):
    bars = auction_bars(evidence=False)
    compact = bars.drop("symbol", "datetime").to_dict(as_series=False)
    compact["timestamp"] = [int(dt.replace(tzinfo=CN_TZ).timestamp() * 1000) for dt in bars["datetime"]]
    fake = SimpleNamespace(intraday_batch=Mock(return_value={SYMBOL: compact}),
                           intraday_universe=Mock(return_value={SYMBOL: compact}))
    monkeypatch.setattr(kline_sync, "get_client", lambda: SimpleNamespace(klines=fake))
    for frame, _ in (kline_sync.fetch_intraday_full_market_burst([SYMBOL], None),
                     kline_sync.fetch_intraday_universe_increment()):
        assert frame["minute_price_basis"].unique().to_list() == ["raw"]
        assert frame["minute_time_grid"].unique().to_list() == ["auction_end"]
    assert "minute_price_basis" not in kline_sync._normalize_minute(bars).columns


def test_partition_merge_keeps_row_evidence_and_unknown_overwrite_clears_old_claim(tmp_path):
    old = auction_bars(evidence=False).head(2)
    store(tmp_path, old)
    path = store(tmp_path, auction_bars().slice(1, 2))
    merged = pl.read_parquet(path).sort("datetime")
    assert merged["minute_price_basis"].to_list() == [None, "raw", "raw"]
    # A subsequent unannotated writer replaces prices and must also erase evidence.
    store(tmp_path, old.tail(1).with_columns(pl.lit(20.).alias("close")))
    replaced = pl.read_parquet(path).sort("datetime")
    assert replaced["minute_price_basis"].to_list() == [None, None, "raw"]
    assert replaced["close"][1] == 20.
    assert not minute_adjust.minute_basis_is_raw(tmp_path)


def test_adapter_reads_new_evidence_without_global_marker_and_late_write_invalidates(tmp_path, monkeypatch):
    path = store(tmp_path, auction_bars())
    repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(inputs, "capability_gate", lambda kind: (True, "configured-provider"))
    adapter = inputs.AttentionData(repo)
    now = stamp(13, 10).replace(tzinfo=CN_TZ)
    frame, raw, reason = adapter.minutes({SYMBOL}, now=now)
    assert raw and reason is None
    assert "minute_time_grid" in frame.columns
    assert intraday(frame)["return_5m"] is not None
    assert adapter.minutes({SYMBOL}, now=now + timedelta(seconds=30))[0] is frame
    # Updated same-minute data clears the claim, even though the values are plausible.
    unconfirmed = auction_bars(evidence=False)
    unconfirmed.write_parquet(path)
    changed, raw, reason = adapter.minutes({SYMBOL}, now=now)
    assert changed.is_empty() and not raw and reason
    assert not minute_adjust.minute_basis_is_raw(tmp_path)


@pytest.mark.parametrize("explicit", ["forward", "unknown"])
def test_global_marker_does_not_override_explicit_non_raw_rows(tmp_path, monkeypatch, explicit):
    frame = auction_bars().with_columns(pl.lit(explicit).alias("minute_price_basis"))
    store(tmp_path, frame)
    minute_adjust.mark_minute_basis_raw(tmp_path)
    repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(inputs, "capability_gate", lambda kind: (True, "configured-provider"))
    result, raw, reason = inputs.AttentionData(repo).minutes(
        {SYMBOL}, now=stamp(13, 10).replace(tzinfo=CN_TZ))
    assert not raw and result.is_empty() and reason


def test_read_projection_cannot_keep_raw_evidence_after_adjusting_prices(tmp_path):
    frame = auction_bars()
    root = tmp_path / "adj_factor"
    root.mkdir()
    pl.DataFrame({"symbol": [SYMBOL], "trade_date": [DAY.date() + timedelta(days=1)],
                  "ex_factor": [1.25]}).write_parquet(root / "all.parquet")
    projected = minute_adjust.apply_minute_adjustment(frame, tmp_path)
    assert projected["close"][0] == pytest.approx(8.)
    assert projected["minute_price_basis"].unique().to_list() == ["forward"]


def test_http_provider_preserves_optional_evidence_but_never_invents_it():
    frame = GenericHTTPProvider._normalize_minute(auction_bars())
    assert frame["minute_price_basis"].unique().to_list() == ["raw"]
    assert frame["minute_time_grid"].unique().to_list() == ["auction_end"]
    legacy = GenericHTTPProvider._normalize_minute(auction_bars(evidence=False))
    assert "minute_price_basis" not in legacy.columns


def test_mixed_stocks_do_not_certify_legacy_rows_or_block_confirmed_stocks(tmp_path, monkeypatch):
    other = "000002.SZ"
    frame = pl.concat([auction_bars(), auction_bars(evidence=False).with_columns(
        pl.lit(other).alias("symbol"))], how="diagonal_relaxed")
    store(tmp_path, frame)
    repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(inputs, "capability_gate", lambda kind: (True, "configured-provider"))
    now = stamp(13, 10).replace(tzinfo=CN_TZ)
    result, raw, reason = inputs.AttentionData(repo).minutes({SYMBOL, other}, now=now)
    assert raw and reason is None and result["symbol"].n_unique() == 2
    evidence = attention._intraday(result, {SYMBOL, other}, now, raw, reason)
    assert evidence[SYMBOL]["return_5m"] is not None
    assert evidence[other]["return_5m"] is None


def test_multi_day_repository_preserves_optional_columns_when_old_partition_is_first(tmp_path):
    legacy = auction_bars(evidence=False).with_columns(pl.col("datetime") - pl.duration(days=1))
    store(tmp_path, legacy)
    store(tmp_path, auction_bars())
    repo = KlineRepository(DataStore(tmp_path))
    frame = repo.get_minute_by_dates([SYMBOL], [DAY.date() - timedelta(days=1), DAY.date()])
    assert frame.height == 482
    old = frame.filter(pl.col("datetime").dt.date() < DAY.date())
    assert old["minute_price_basis"].null_count() == old.height
    assert frame.filter(pl.col("datetime").dt.date() == DAY.date())["minute_price_basis"].unique().to_list() == ["raw"]


def test_optional_grid_nulls_from_other_symbol_do_not_reject_declared_raw_240_bars():
    other = "000002.SZ"
    standard = auction_bars().filter(pl.col("datetime") != stamp(9, 30)).drop("minute_time_grid")
    standard = standard.with_columns(pl.lit(other).alias("symbol"))
    mixed = pl.concat([auction_bars(), standard], how="diagonal_relaxed")
    now = stamp(13, 10).replace(tzinfo=CN_TZ)
    results = attention._intraday(mixed, {SYMBOL, other}, now, True, None)
    assert all(item["return_5m"] is not None for item in results.values())


@pytest.mark.parametrize("reader", ["single", "batch", "range"])
@pytest.mark.parametrize("asset", ["stock", "etf"])
def test_public_minute_readers_accept_old_and_new_partition_schemas(tmp_path, reader, asset):
    minute_dir = tmp_path / ("kline_etf_minute" if asset == "etf" else "kline_minute")
    old_day = DAY.date() - timedelta(days=1)
    old = auction_bars(evidence=False).with_columns(pl.col("datetime") - pl.duration(days=1))
    kline_sync._write_minute_partition(old, minute_dir)
    kline_sync._write_minute_partition(auction_bars(), minute_dir)
    repo = KlineRepository(DataStore(tmp_path))
    if reader == "single":
        frame = repo.get_minute(SYMBOL, DAY.date(), asset)
    elif reader == "batch":
        frame = repo.get_minute_batch([SYMBOL], DAY.date(), asset)
    else:
        frame = repo.get_minute_range([SYMBOL], old_day, DAY.date(), asset)
    assert frame.height == (482 if reader == "range" else 241)
    if reader != "range":
        assert frame["minute_price_basis"].unique().to_list() == ["raw"]
        assert repo._minute_partition_path(asset, DAY.date()) is not None


@pytest.mark.parametrize("new_first", [False, True])
@pytest.mark.parametrize("reader", ["single", "batch"])
def test_legacy_layout_fallback_preserves_new_evidence(tmp_path, new_first, reader):
    directory = tmp_path / "kline_minute" / f"symbol={SYMBOL}"
    directory.mkdir(parents=True)
    old = auction_bars(evidence=False).with_columns(pl.col("datetime") - pl.duration(days=1))
    frames = [auction_bars(), old] if new_first else [old, auction_bars()]
    for i, frame in enumerate(frames):
        frame.write_parquet(directory / f"{i}.parquet")
    repo = KlineRepository(DataStore(tmp_path))
    result = (repo.get_minute(SYMBOL, DAY.date()) if reader == "single"
              else repo.get_minute_batch([SYMBOL], DAY.date()))
    assert result.height == 241
    assert result["minute_price_basis"].unique().to_list() == ["raw"]


def test_legacy_layout_fallback_does_not_accept_missing_basic_columns(tmp_path):
    directory = tmp_path / "kline_minute" / f"symbol={SYMBOL}"
    directory.mkdir(parents=True)
    old = auction_bars(evidence=False).with_columns(pl.col("datetime") - pl.duration(days=1))
    old.write_parquet(directory / "0.parquet")
    auction_bars().drop("close").write_parquet(directory / "1.parquet")
    repo = KlineRepository(DataStore(tmp_path))
    assert repo.get_minute(SYMBOL, DAY.date()).is_empty()


@pytest.mark.parametrize("missing", [False, True])
def test_legacy_layout_fallback_keeps_optional_amount_semantics(tmp_path, missing):
    directory = tmp_path / "kline_minute" / f"symbol={SYMBOL}"
    directory.mkdir(parents=True)
    frame = auction_bars(evidence=False)
    frame = frame.drop("amount") if missing else frame.with_columns(pl.lit(None, dtype=pl.Float64).alias("amount"))
    frame.write_parquet(directory / "0.parquet")
    repo = KlineRepository(DataStore(tmp_path))
    assert repo.get_minute(SYMBOL, DAY.date()).height == 241


@pytest.mark.parametrize("asset", ["stock", "etf"])
def test_writer_preserves_existing_migration_evidence_without_certifying_new_rows(tmp_path, asset):
    directory = tmp_path / ("kline_etf_minute" if asset == "etf" else "kline_minute")
    old = auction_bars(evidence=False).filter(pl.col("datetime") != stamp(9, 30))
    kline_sync._write_minute_partition(old, directory)
    minute_adjust.mark_minute_basis_raw(tmp_path)
    newcomer = auction_bars().with_columns(pl.lit("000002.SZ").alias("symbol"))
    kline_sync._write_minute_partition(newcomer, directory)
    path = directory / f"date={DAY.date()}" / "part.parquet"
    saved = pl.read_parquet(path)
    legacy = saved.filter(pl.col("symbol") == SYMBOL)
    assert legacy["minute_price_basis"].unique().to_list() == ["raw"]
    assert intraday(legacy)["return_5m"] is not None
    # The marker belongs to old stored facts. A new unknown OHLC overwrites it.
    kline_sync._write_minute_partition(old.head(1), directory)
    replaced = pl.read_parquet(path).filter(pl.col("symbol") == SYMBOL)
    assert replaced["minute_price_basis"][0] is None
    # Later writes cannot reinterpret an existing null as old absent metadata.
    kline_sync._write_minute_partition(newcomer, directory)
    persisted = pl.read_parquet(path).filter(pl.col("symbol") == SYMBOL)
    assert persisted["minute_price_basis"][0] is None
