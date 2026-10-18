from datetime import date, timedelta

import polars as pl
import pytest

from app.services.huichun_audit import SuspensionEvidence, load_suspensions, prepare_batch


def raw_rows(days, *, closes=None):
    closes = [float(c) for c in closes] if closes is not None else [10.0] * len(days)
    return pl.DataFrame(
        {
            "symbol": ["600000.SH"] * len(days),
            "date": days,
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [100.0] * len(days),
            "amount": [1000.0] * len(days),
        }
    )


def factors(days=(), values=()):
    return pl.DataFrame(
        {"symbol": ["600000.SH"] * len(days), "trade_date": list(days), "ex_factor": list(values)},
        schema={"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64},
    )


@pytest.fixture
def days():
    return [date(2024, 1, 2) + timedelta(days=i) for i in range(5)]


def prepare(raw, fac, days, **kwargs):
    return prepare_batch(
        raw,
        fac,
        market_dates=days,
        end=days[-1],
        listing_dates=kwargs.get("listing_dates", {}),
        suspensions=kwargs.get("suspensions", ()),
    )


def suspension(start, end=None, known_at=None):
    return SuspensionEvidence(
        symbol="600000.SH",
        start_date=start,
        end_date=end or start,
        known_at=known_at or (start - timedelta(days=1)),
        source_url="https://example.org/announcement.pdf",
        title="Full-day suspension announcement",
    )


def evidence_json(day):
    return {
        "symbol": "600000.SH",
        "start_date": str(day),
        "end_date": str(day),
        "known_at": str(day - timedelta(days=1)),
        "source_url": "https://example.org/announcement.pdf",
        "title": "Full-day suspension announcement",
        "event_type": "full_day_suspension",
    }


def calendar_json(days):
    return {
        "version": 1,
        "coverage_start": str(days[0]),
        "coverage_end": str(days[-1]),
        "weekends_closed": True,
        "sources": [
            {
                "id": "sse-2024",
                "url": "https://www.sse.com.cn/example.shtml",
                "published_on": "2023-12-22",
                "title": "2024 exchange holiday notice",
            }
        ],
        "closures": [],
    }


def test_missing_observed_day_resets_segment_and_trailing_gap_is_invalidated(days):
    result = prepare(raw_rows([days[0], days[1], days[3]]), factors(), days)
    assert result.frame["segment_id"].to_list() == [0, 0, 1]
    assert result.frame["segment_invalidated_at"].to_list() == [None, days[2], days[4]]
    row = result.coverage.row(0, named=True)
    assert row["missing_observed_days"] == 2
    assert row["largest_segment_bars"] == 2
    assert row["confirmed_suspension_days"] == 0


def test_confirmed_suspension_bridges_segment_without_inventing_bars(days):
    present = [days[0], days[1], days[3], days[4]]
    result = prepare(raw_rows(present), factors(), days, suspensions=(suspension(days[2]),))
    assert result.frame["date"].to_list() == present
    assert result.frame["segment_id"].to_list() == [0, 0, 0, 0]
    assert result.frame["segment_invalidated_at"].null_count() == 4
    assert result.frame["eligible"].null_count() == 4
    row = result.coverage.row(0, named=True)
    assert row["confirmed_suspension_days"] == 1
    assert row["missing_observed_days"] == 0
    assert row["largest_segment_bars"] == 4


@pytest.mark.parametrize("known_offset, confirmed", [(-1, 1), (0, 0), (1, 0)])
def test_date_only_announcements_cannot_remove_same_day_or_prior_gaps(
    days, known_offset, confirmed
):
    missing_day = days[2]
    evidence = suspension(missing_day, known_at=missing_day + timedelta(days=known_offset))
    result = prepare(
        raw_rows([days[0], days[1], days[3], days[4]]),
        factors(),
        days,
        suspensions=(evidence,),
    )
    row = result.coverage.row(0, named=True)
    assert row["confirmed_suspension_days"] == confirmed
    assert row["missing_observed_days"] == 1 - confirmed
    assert row["suspension_days_without_prior_evidence"] == 1 - confirmed
    assert result.frame["segment_id"].to_list() == [0, 0, 1 - confirmed, 1 - confirmed]


def test_duplicate_evidence_deduplicates_days_and_unknown_between_halts_still_breaks(days):
    evidence = (suspension(days[1]), suspension(days[1]), suspension(days[3]))
    result = prepare(raw_rows([days[0], days[4]]), factors(), days, suspensions=evidence)
    assert result.frame["segment_id"].to_list() == [0, 1]
    assert result.frame["segment_invalidated_at"].to_list() == [days[2], None]
    assert result.coverage["confirmed_suspension_days"].item() == 2
    assert result.coverage["missing_observed_days"].item() == 1


def test_tail_skips_confirmed_suspensions_until_first_unknown_date(days):
    raw = raw_rows(days[:2])
    result = prepare(raw, factors(), days, suspensions=(suspension(days[2]), suspension(days[4])))
    assert result.frame["segment_invalidated_at"].to_list() == [None, days[3]]
    all_confirmed = prepare(raw, factors(), days, suspensions=(suspension(days[2], end=days[-1]),))
    assert all_confirmed.frame["segment_invalidated_at"].null_count() == 2
    assert all_confirmed.coverage["missing_observed_days"].item() == 0


@pytest.mark.parametrize("known_offset", [-1, 0, 1])
def test_confirmed_full_day_suspension_conflicting_with_valid_price_fails(days, known_offset):
    evidence = suspension(days[2], known_at=days[2] + timedelta(days=known_offset))
    with pytest.raises(ValueError, match="conflict"):
        prepare(raw_rows(days), factors(), days, suspensions=(evidence,))


def test_suspension_days_do_not_count_toward_250_valid_bar_warmup():
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(260)]
    present = [*dates[:120], *dates[131:]]
    result = prepare(
        raw_rows(present),
        factors(),
        dates,
        suspensions=(suspension(dates[120], end=dates[130]),),
    )
    assert result.coverage["largest_segment_bars"].item() == 249
    assert result.coverage["has_250_bar_segment"].item() is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"symbol": "600000"},
        {"start_date": "2024-01-02T09:00:00"},
        {"known_at": "20240101"},
        {"event_type": "intraday_suspension"},
        {"source_url": "file:///announcement.pdf"},
        {"title": " "},
        {"end_date": "2023-01-01"},
        {"unexpected": "extra"},
    ],
)
def test_suspension_evidence_contract_rejects_ambiguous_or_incomplete_records(
    tmp_path, days, overrides
):
    import json

    path = tmp_path / "suspensions.json"
    path.write_text(json.dumps([{**evidence_json(days[1]), **overrides}]), encoding="utf-8")
    with pytest.raises(ValueError):
        load_suspensions(path)


def test_suspension_evidence_loader_accepts_strict_records_and_rejects_missing_keys(tmp_path, days):
    import json

    path = tmp_path / "suspensions.json"
    record = evidence_json(days[1])
    path.write_text(json.dumps([record]), encoding="utf-8")
    assert load_suspensions(path) == (suspension(days[1]),)
    del record["known_at"]
    path.write_text(json.dumps([record]), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly"):
        load_suspensions(path)


def test_duplicate_day_is_not_arbitrarily_deduplicated(days):
    result = prepare(raw_rows([days[0], days[1], days[1], days[2]]), factors(), days)
    assert days[1] not in result.frame["date"].to_list()
    assert result.coverage["duplicate_rows"].item() == 2
    assert result.frame["segment_id"].to_list() == [0, 1]


def test_invalid_ohlc_and_zero_volume_are_unknown_not_confirmed_halts(days):
    raw = raw_rows(days).with_columns(
        pl.when(pl.col("date") == days[1]).then(0).otherwise(pl.col("volume")).alias("volume"),
        pl.when(pl.col("date") == days[2]).then(5).otherwise(pl.col("high")).alias("high"),
    )
    result = prepare(raw, factors(), days)
    assert result.coverage["invalid_rows"].item() == 2
    assert result.frame["date"].to_list() == [days[0], days[3], days[4]]


def test_causal_adjustment_uses_event_date_and_future_does_not_rewrite_past(days):
    raw = raw_rows(days, closes=[10, 10, 5, 5, 2.5])
    result = prepare(raw, factors([days[2], days[4]], [2, 2]), days)
    assert result.frame["close"].to_list() == [10] * 5
    prefix = prepare(raw.head(4), factors([days[2], days[4]], [2, 100]), days[:4])
    assert prefix.frame["close"].to_list() == result.frame["close"].to_list()[:4]
    assert result.frame["eligible"].null_count() == 5


def test_invalid_or_ambiguous_factor_excludes_price_audit(days):
    for fac in (factors([days[1]], [-1]), factors([days[1], days[1]], [1.1, 1.2])):
        result = prepare(raw_rows(days), fac, days)
        assert result.frame.is_empty()
        assert result.coverage["factor_status"].item() == "invalid"


def test_pre_listing_days_are_not_counted_missing(days):
    result = prepare(raw_rows(days[2:]), factors(), days, listing_dates={"600000.SH": days[2]})
    assert result.coverage["missing_observed_days"].item() == 0


def test_unknown_listing_date_does_not_claim_history_complete(days):
    result = prepare(raw_rows(days[2:]), factors(), days)
    assert result.coverage["missing_observed_days"].item() == 2
    assert result.coverage["listing_date_known"].item() is False


def test_missing_stock_still_appears_in_coverage(days):
    result = prepare_batch(
        raw_rows(days).head(0),
        factors(),
        market_dates=days,
        end=days[-1],
        listing_dates={"600000.SH": days[0]},
        expected_symbols=["600000.SH"],
    )
    assert result.coverage["valid_rows"].item() == 0
    assert result.frame.is_empty()


def test_snapshot_rows_before_market_close_are_excluded(days):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ts = int(datetime(2024, 1, 2, 10, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
    raw = raw_rows(days).with_columns(
        pl.when(pl.col("date") == days[0]).then(ts).otherwise(None).alias("quote_ts")
    )
    result = prepare(raw, factors(), days)
    assert result.coverage["invalid_rows"].item() == 1
    assert days[0] not in result.frame["date"].to_list()


def test_run_audit_zero_signal_outputs_keep_schema_and_do_not_modify_inputs(tmp_path, days):
    import json

    from app.services.huichun_audit import run_audit, snapshot_files
    from app.tickflow.repository import DataStore, KlineRepository

    store = DataStore(tmp_path / "data")
    try:
        raw_rows(days).write_parquet(store.data_dir / "kline_daily" / "sample.parquet")
        pl.DataFrame(
            {"symbol": ["600000.SH"], "name": ["sample"], "listing_date": [str(days[0])]}
        ).write_parquet(store.data_dir / "instruments" / "sample.parquet")
        store._register_views()
        before = snapshot_files(store.data_dir)
        output = tmp_path / "audit"
        result = run_audit(KlineRepository(store), start=days[0], end=days[-1], output_dir=output)
        assert result["shape_candidates"] == 0
        assert result["calendar_source"] == "observed_daily_dates_lower_bound"
        assert result["expected_market_days"] == result["observed_market_days"] == len(days)
        assert result["market_dates_without_observations"] == []
        exported = pl.read_csv(output / "a0_signals.csv")
        assert exported.is_empty()
        assert "strict_signal" in exported.columns
        assert "status" in pl.read_csv(output / "a0_cycles.csv").columns
        assert (output / "coverage_report.md").is_file()
        manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
        assert manifest["parameters"]["warmup_bars"] == 250
        assert manifest["inputs"] == before == snapshot_files(store.data_dir)
        with pytest.raises(ValueError, match="new"):
            run_audit(KlineRepository(store), start=days[0], end=days[-1], output_dir=output)
    finally:
        store.db.close()


def test_changed_inputs_abort_before_report_publication(tmp_path, days, monkeypatch):
    from app.services import huichun_audit as service
    from app.tickflow.repository import DataStore, KlineRepository

    store = DataStore(tmp_path / "data")
    try:
        raw_rows(days).write_parquet(store.data_dir / "kline_daily" / "sample.parquet")
        store._register_views()
        snapshots = iter([{"sha256": "before"}, {"sha256": "after"}])
        monkeypatch.setattr(service, "snapshot_files", lambda _: next(snapshots))
        output = tmp_path / "audit"
        with pytest.raises(RuntimeError, match="changed"):
            service.run_audit(
                KlineRepository(store), start=days[0], end=days[-1], output_dir=output
            )
        assert not output.exists()
    finally:
        store.db.close()


def test_run_audit_hashes_suspension_sources_and_reports_known_and_late_evidence(tmp_path, days):
    import json

    from app.services.huichun_audit import run_audit, snapshot_files
    from app.tickflow.repository import DataStore, KlineRepository

    store = DataStore(tmp_path / "data")
    try:
        anchor = raw_rows(days).with_columns(pl.lit("000001.SZ").alias("symbol"))
        raw = pl.concat([anchor, raw_rows([days[0], days[3], days[4]])])
        raw.write_parquet(store.data_dir / "kline_daily" / "sample.parquet")
        store._register_views()
        path = tmp_path / "suspensions.json"
        path.write_text(
            json.dumps(
                [
                    evidence_json(days[1]),
                    {**evidence_json(days[2]), "known_at": str(days[2])},
                ]
            ),
            encoding="utf-8",
        )
        before = snapshot_files(store.data_dir, suspensions_path=path)
        output = tmp_path / "audit"
        summary = run_audit(
            KlineRepository(store),
            start=days[0],
            end=days[-1],
            output_dir=output,
            suspensions_path=path,
        )
        assert summary["confirmed_suspension_days"] == 1
        assert summary["symbols_with_confirmed_suspensions"] == 1
        assert summary["suspension_days_without_prior_evidence"] == 1
        manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
        assert manifest["inputs"] == before
        assert any(row["path"].startswith("suspensions:") for row in before["files"])
        assert len(manifest["suspension_evidence"]["records"]) == 2
        coverage = pl.read_parquet(output / "coverage_by_symbol.parquet")
        assert coverage.filter(pl.col("symbol") == "600000.SH")["missing_observed_days"].item() == 1
    finally:
        store.db.close()


def test_changed_suspension_evidence_aborts_before_report_publication(tmp_path, days, monkeypatch):
    from app.services import huichun_audit as service
    from app.tickflow.repository import DataStore, KlineRepository

    store = DataStore(tmp_path / "data")
    try:
        raw_rows(days).write_parquet(store.data_dir / "kline_daily" / "sample.parquet")
        store._register_views()
        path = tmp_path / "suspensions.json"
        path.write_text("[]", encoding="utf-8")
        original = service.prepare_batch

        def change_evidence(*args, **kwargs):
            result = original(*args, **kwargs)
            path.write_text("[ ]", encoding="utf-8")
            return result

        monkeypatch.setattr(service, "prepare_batch", change_evidence)
        output = tmp_path / "audit"
        with pytest.raises(RuntimeError, match="changed"):
            service.run_audit(
                KlineRepository(store),
                start=days[0],
                end=days[-1],
                output_dir=output,
                suspensions_path=path,
            )
        assert not output.exists()
    finally:
        store.db.close()


def test_official_calendar_detects_marketwide_missing_days_and_preserves_raw_bounds(tmp_path, days):
    import json

    from app.services.huichun_audit import run_audit
    from app.tickflow.repository import DataStore, KlineRepository

    trading_days = days[:4]  # Tuesday through Friday.
    store = DataStore(tmp_path / "data")
    try:
        raw_rows([trading_days[0], trading_days[2]]).write_parquet(
            store.data_dir / "kline_daily" / "sample.parquet"
        )
        store._register_views()
        path = tmp_path / "calendar.json"
        path.write_text(json.dumps(calendar_json(trading_days)), encoding="utf-8")
        output = tmp_path / "audit"
        summary = run_audit(
            KlineRepository(store),
            start=trading_days[0],
            end=trading_days[-1],
            output_dir=output,
            calendar_path=path,
        )
        assert summary["raw_first"] == str(trading_days[0])
        assert summary["raw_last"] == str(trading_days[2])
        assert summary["observed_market_days"] == 2
        assert summary["expected_market_days"] == 4
        assert summary["market_days_without_observations"] == 2
        assert summary["market_dates_without_observations"] == [
            str(trading_days[1]),
            str(trading_days[3]),
        ]
        assert summary["calendar_source"] == "official_exchange_holiday_notices"
        assert summary["eligibility_status"] == "unverified"
        coverage = pl.read_parquet(output / "coverage_by_symbol.parquet")
        assert coverage["missing_observed_days"].item() == 2
        assert coverage["segment_count"].item() == 2
        manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
        assert "huichun_calendar.py" in manifest["source_code_sha256"]
        assert any(row["path"].startswith("calendar:") for row in manifest["inputs"]["files"])
        assert "独立交易日历已依据交易所休市公告核验" in (output / "coverage_report.md").read_text(
            encoding="utf-8"
        )
    finally:
        store.db.close()


def test_official_calendar_rejects_raw_prices_on_closed_day(tmp_path, days):
    import json

    from app.services.huichun_audit import run_audit
    from app.tickflow.repository import DataStore, KlineRepository

    store = DataStore(tmp_path / "data")
    try:
        raw_rows(days).write_parquet(store.data_dir / "kline_daily" / "sample.parquet")
        store._register_views()
        path = tmp_path / "calendar.json"
        path.write_text(json.dumps(calendar_json(days)), encoding="utf-8")
        output = tmp_path / "audit"
        with pytest.raises(ValueError, match="Raw dates conflict"):
            run_audit(
                KlineRepository(store),
                start=days[0],
                end=days[-1],
                output_dir=output,
                calendar_path=path,
            )
        assert not output.exists()
    finally:
        store.db.close()


def test_changed_official_calendar_aborts_before_report_publication(tmp_path, days, monkeypatch):
    import json

    from app.services import huichun_audit as service
    from app.tickflow.repository import DataStore, KlineRepository

    trading_days = days[:4]
    store = DataStore(tmp_path / "data")
    try:
        raw_rows(trading_days).write_parquet(store.data_dir / "kline_daily" / "sample.parquet")
        store._register_views()
        path = tmp_path / "calendar.json"
        content = json.dumps(calendar_json(trading_days))
        path.write_text(content, encoding="utf-8")
        original = service.prepare_batch

        def change_calendar(*args, **kwargs):
            result = original(*args, **kwargs)
            assert result.frame["eligible"].null_count() == result.frame.height
            assert result.frame["eligibility_reason"].unique().to_list() == [
                "historical_status_and_factor_completeness_unverified"
            ]
            path.write_text(content + " ", encoding="utf-8")
            return result

        monkeypatch.setattr(service, "prepare_batch", change_calendar)
        output = tmp_path / "audit"
        with pytest.raises(RuntimeError, match="changed"):
            service.run_audit(
                KlineRepository(store),
                start=trading_days[0],
                end=trading_days[-1],
                output_dir=output,
                calendar_path=path,
            )
        assert not output.exists()
    finally:
        store.db.close()


def test_research_factor_replacement_is_hashed_and_does_not_merge_repository(tmp_path, days):
    import json

    from app.services.huichun_audit import run_audit, snapshot_files
    from app.tickflow.repository import DataStore, KlineRepository

    store = DataStore(tmp_path / "data")
    try:
        raw_rows(days).write_parquet(store.data_dir / "kline_daily/sample.parquet")
        factors([days[2]], [2.0]).write_parquet(store.data_dir / "adj_factor/sample.parquet")
        store._register_views()
        before = snapshot_files(store.data_dir)
        replacement = tmp_path / "replacement.parquet"
        factors([]).write_parquet(replacement)
        output = tmp_path / "audit"
        run_audit(
            KlineRepository(store),
            start=days[0],
            end=days[-1],
            output_dir=output,
            factors_path=replacement,
        )
        assert snapshot_files(store.data_dir) == before
        coverage = pl.read_parquet(output / "coverage_by_symbol.parquet")
        assert coverage["factor_events"].sum() == 0
        manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
        assert any(r["path"].startswith("factors:") for r in manifest["inputs"]["files"])
    finally:
        store.db.close()
