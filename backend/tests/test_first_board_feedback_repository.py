from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.enriched_generation import EnrichedGenerationUnavailableError, EnrichedPublication
from app.market_time import CN_TZ
from app.tickflow.repository import KlineRepository

DAYS = [date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)]


def partition(repo, day, *, raw=10.0, adjusted=None, stamp=None, symbol="600000.SH"):
    path = repo.store.data_dir / "kline_daily_enriched" / f"date={day}" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame({"symbol": [symbol], "date": [day], "raw_close": [raw],
                          "close": [raw if adjusted is None else adjusted], "quote_ts": [stamp]},
                         schema_overrides={"quote_ts": pl.Int64})
    frame.write_parquet(path)
    return frame


@pytest.fixture
def repo(tmp_path, monkeypatch):
    from app.indicators import pipeline

    monkeypatch.setattr(pipeline, "_get_custom_signal_exprs", lambda: {})
    result = KlineRepository(SimpleNamespace(data_dir=tmp_path, db=None))
    path = tmp_path / "instruments" / "instruments.parquet"
    path.parent.mkdir()
    pl.DataFrame({"symbol": ["600000.SH"], "name": ["测试股票"],
                  "listing_date": [date(2000, 1, 1)], "as_of": [date(2026, 10, 26)],
                  "limit_up": [999.0]}).write_parquet(path)
    for day in DAYS:
        partition(result, day, raw=11.0 if day == DAYS[-1] else 10.0)
    return result


def test_completed_sample_survives_real_realtime_flush_and_cold_history(repo):
    frame, token = repo.get_first_board_history(DAYS)
    assert frame["date"].to_list() == DAYS[1:]
    assert frame["limit_up_price"].to_list() == pytest.approx([10.995, 10.995])
    assert frame["name"].to_list() == ["测试股票", "测试股票"]
    generation = repo.get_matrix_data_generation("stock")
    live = pl.DataFrame({"symbol": ["600000.SH"], "date": [date(2026, 10, 26)],
                         "raw_close": [12.1], "close": [12.1]})
    repo.flush_live_enriched_asset("stock", live)
    assert repo.get_matrix_data_generation("stock") != generation
    assert repo._enriched_history_cache is None
    assert repo.get_first_board_history_signature(DAYS) == token
    after, same = repo.get_first_board_history(DAYS)
    assert same == token
    assert after.equals(frame)


def test_only_requested_partitions_are_read_and_history_correction_invalidates(repo):
    unrelated = repo.store.data_dir / "kline_daily_enriched" / "date=2000-01-01"
    unrelated.mkdir()
    (unrelated / "part.parquet").write_bytes(b"not parquet")
    _, token = repo.get_first_board_history(DAYS)
    partition(repo, DAYS[-1], raw=10.5)
    corrected, changed = repo.get_first_board_history(DAYS)
    assert changed != token
    assert corrected["raw_close"].to_list() == [10.0, 10.5]


def test_target_pair_scale_handles_ex_rights_and_later_uniform_adjustment(repo):
    partition(repo, DAYS[0], raw=10, adjusted=2.5)
    partition(repo, DAYS[1], raw=5, adjusted=2.5)
    partition(repo, DAYS[2], raw=5.5, adjusted=2.75)
    frame, _ = repo.get_first_board_history(DAYS)
    assert frame["limit_up_price"].to_list() == pytest.approx([5.495, 5.495])


def test_missing_previous_price_and_incomplete_previous_close_do_not_prove_limit(repo):
    stamp = int(datetime(2026, 10, 21, 14, 55, tzinfo=CN_TZ).timestamp() * 1000)
    partition(repo, DAYS[0], stamp=stamp)
    frame, _ = repo.get_first_board_history(DAYS)
    assert frame["limit_up_price"][0] is None
    assert not frame["limit_evidence_known"][0]
    partition(repo, DAYS[0], raw=float("nan"))
    frame, _ = repo.get_first_board_history(DAYS)
    assert frame["limit_up_price"][0] is None


def test_undated_current_authoritative_price_never_overrides_yesterday(repo):
    path = repo.store.data_dir / "instruments" / "instruments.parquet"
    pl.DataFrame({"symbol": ["600000.SH"], "name": ["测试股票"],
                  "listing_date": [date(2000, 1, 1)], "limit_up": [999.0]}).write_parquet(path)
    frame, _ = repo.get_first_board_history(DAYS)
    assert frame["limit_up_price"].to_list() == pytest.approx([10.995, 10.995])


def test_missing_partition_and_unfinished_publication_fail_closed(repo):
    with pytest.raises(ValueError):
        repo.get_first_board_history_signature(DAYS[::-1])
    with pytest.raises(FileNotFoundError):
        repo.get_first_board_history([date(2026, 10, 20), *DAYS[:2]])
    publication = EnrichedPublication(repo.store.data_dir, "stock")
    publication.begin()
    try:
        with pytest.raises(EnrichedGenerationUnavailableError):
            repo.get_first_board_history_signature(DAYS)
    finally:
        publication.abandon()


def test_reader_rejects_partition_change_during_limit_computation(repo, monkeypatch):
    from app.indicators import pipeline

    original = pipeline.compute_limit_signals

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        partition(repo, DAYS[-1], raw=10.25)
        return result

    monkeypatch.setattr(pipeline, "compute_limit_signals", changed)
    with pytest.raises(EnrichedGenerationUnavailableError, match="changed"):
        repo.get_first_board_history(DAYS)


@pytest.mark.parametrize(("symbol", "listing", "expected"), [
    ("300001.SZ", date(2000, 1, 1), 11.995),
    ("688001.SH", date(2000, 1, 1), 11.995),
    ("600000.SH", date(2026, 10, 21), None),
    ("600000.SH", None, None),
])
def test_board_rules_no_limit_window_and_missing_listing(repo, symbol, listing, expected):
    path = repo.store.data_dir / "instruments" / "instruments.parquet"
    pl.DataFrame({"symbol": [symbol], "name": ["测试股票"], "listing_date": [listing]},
                 schema_overrides={"listing_date": pl.Date}).write_parquet(path)
    for day in DAYS:
        partition(repo, day, raw=10.0, symbol=symbol)
    frame, _ = repo.get_first_board_history(DAYS)
    if expected is None:
        assert frame["limit_up_price"].null_count() == 2
        assert frame["limit_evidence_known"].to_list() == [listing is not None] * 2
    else:
        assert frame["limit_up_price"].to_list() == pytest.approx([expected, expected])


def test_duplicate_previous_rows_cannot_produce_a_known_limit(repo):
    prior = partition(repo, DAYS[0])
    path = repo.store.data_dir / "kline_daily_enriched" / f"date={DAYS[0]}" / "part.parquet"
    pl.concat([prior, prior]).write_parquet(path)
    frame, _ = repo.get_first_board_history(DAYS)
    assert frame["limit_up_price"][0] is None


def test_duplicate_sample_rows_remain_unknown_for_coverage_reporting(repo):
    sample = partition(repo, DAYS[-1])
    path = repo.store.data_dir / "kline_daily_enriched" / f"date={DAYS[-1]}" / "part.parquet"
    pl.concat([sample, sample]).write_parquet(path)
    frame, _ = repo.get_first_board_history(DAYS)
    row = frame.filter(pl.col("date") == DAYS[-1]).row(0, named=True)
    assert row["limit_up_price"] is None
    assert row["limit_evidence_known"] is False


def test_metadata_updates_change_signature_without_reusing_instrument_cache(repo):
    _, token = repo.get_first_board_history(DAYS)
    path = repo.store.data_dir / "instruments" / "instruments.parquet"
    instruments = pl.read_parquet(path)
    repo._instruments_cache = instruments
    instruments.with_columns(pl.lit("已改名").alias("name")).write_parquet(path)
    frame, changed = repo.get_first_board_history(DAYS)
    assert changed != token
    assert frame["name"].to_list() == ["已改名", "已改名"]


def test_recent_ipo_fifth_and_sixth_sessions_use_independent_calendar(repo):
    path = repo.store.data_dir / "instruments" / "instruments.parquet"
    pl.DataFrame({"symbol": ["600000.SH"], "name": ["新股"],
                  "listing_date": [date(2026, 10, 16)]}).write_parquet(path)
    uncertain, without_calendar = repo.get_first_board_history(DAYS)
    assert uncertain["limit_evidence_known"].to_list() == [False, False]
    for day in (16, 19, 20, 21, 22, 23):
        trading_day = date(2026, 10, day)
        index = repo.store.data_dir / "kline_index_enriched" / f"date={trading_day}" / "part.parquet"
        index.parent.mkdir(parents=True)
        pl.DataFrame({"symbol": ["000001.SH"], "date": [trading_day]}).write_parquet(index)
    frame, with_calendar = repo.get_first_board_history(DAYS)
    assert without_calendar != with_calendar
    assert frame["limit_up_price"][0] is None  # fifth session: no limit
    assert frame["limit_up_price"][1] == pytest.approx(10.995)  # sixth session
    assert frame["limit_evidence_known"].to_list() == [True, True]
