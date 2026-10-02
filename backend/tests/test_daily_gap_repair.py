from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import daily_gap_repair as repair
from app.tickflow.repository import DataStore, KlineRepository


def daily(symbol="001222.SZ", day=date(2022, 8, 18), close=10.0):
    return pl.DataFrame(
        {
            "symbol": [symbol],
            "date": [day],
            "open": [10.0],
            "high": [max(11.0, close)],
            "low": [9.0],
            "close": [close],
            "volume": [100.0],
            "amount": [100000.0],
        }
    )


def ranges():
    return pl.DataFrame(
        {"symbol": ["001222.SZ"], "start": [date(2022, 8, 18)], "end": [date(2025, 9, 9)]}
    )


def test_prefix_ranges_exclude_unknown_listing_and_other_gaps():
    cov = pl.DataFrame(
        {
            "symbol": ["a", "b", "c", "d"],
            "first_date": [date(2025, 9, 10)] * 3 + [date(2025, 9, 11)],
            "listing_date": [date(2018, 1, 1), None, date(2025, 9, 10), date(2021, 1, 1)],
        }
    )
    result = repair.prefix_ranges(cov, first_date=date(2025, 9, 10), floor=date(2019, 9, 12))
    assert result.to_dicts() == [
        {"symbol": "a", "start": date(2019, 9, 12), "end": date(2025, 9, 9)}
    ]


def test_select_rows_filters_scope_and_rejects_invalid_or_conflicting_duplicates():
    assert repair.select_rows(pl.concat([daily(), daily("600000.SH")]), ranges()).height == 1
    with pytest.raises(ValueError, match="Invalid"):
        repair.select_rows(daily().with_columns(pl.lit(-1.0).alias("volume")), ranges())
    with pytest.raises(ValueError, match="Conflicting"):
        repair.select_rows(pl.concat([daily(), daily(close=10.5)]), ranges())
    assert repair.select_rows(pl.concat([daily(), daily()]), ranges()).height == 1


def test_factor_merge_preserves_existing_and_rejects_conflicts():
    old = pl.DataFrame({"symbol": ["a"], "trade_date": [date(2022, 1, 1)], "ex_factor": [1.1]})
    merged, added = repair.merge_factors(old, old)
    assert added == 0 and merged.equals(old)
    with pytest.raises(ValueError, match="factor conflict"):
        repair.merge_factors(old, old.with_columns(pl.lit(1.2).alias("ex_factor")))


def test_prepare_provider_failure_never_changes_daily(tmp_path):
    store = DataStore(tmp_path / "data")
    repo = KlineRepository(store)
    repo.append_daily(daily(day=date(2025, 9, 10)))
    before = repair.file_hash(next((tmp_path / "data/kline_daily").rglob("*.parquet")))

    def broken(*args, **kwargs):
        yield daily()
        raise RuntimeError("source failed")

    provider = SimpleNamespace(iter_daily=broken)
    with pytest.raises(RuntimeError, match="source failed"):
        repair.prepare_repair(
            repo, provider, ranges(), tmp_path / "plan", factor_end=date(2026, 9, 30)
        )
    assert repair.file_hash(next((tmp_path / "data/kline_daily").rglob("*.parquet"))) == before
    store.db.close()


def make_plan(repo, path):
    provider = SimpleNamespace(
        name="fixture",
        iter_daily=lambda *a, **kw: iter([daily()]),
        get_adj_factors=lambda *a, **kw: pl.DataFrame(schema=repair.FACTOR_SCHEMA),
    )
    return repair.prepare_repair(repo, provider, ranges(), path, factor_end=date(2026, 9, 30))


def test_apply_backups_preserves_old_rows_and_is_idempotent(tmp_path, monkeypatch):
    store = DataStore(tmp_path / "data")
    repo = KlineRepository(store)
    repo.append_daily(daily("600000.SH", close=10.5))
    original = next((tmp_path / "data/kline_daily").rglob("*.parquet")).read_bytes()
    make_plan(repo, tmp_path / "plan")
    recomputed = []
    monkeypatch.setattr(repair, "recompute", lambda repo, syms: recomputed.extend(syms))
    result = repair.apply_repair(repo, tmp_path / "plan", tmp_path / "apply")
    assert result["added_daily_rows"] == 1
    assert recomputed == ["001222.SZ"]
    assert (
        tmp_path / "apply/backup/kline_daily/date=2022-08-18/part.parquet"
    ).read_bytes() == original
    actual = pl.read_parquet(next((tmp_path / "data/kline_daily").rglob("*.parquet")))
    assert actual.filter(pl.col("symbol") == "600000.SH")["close"][0] == 10.5
    second = repair.apply_repair(repo, tmp_path / "plan", tmp_path / "again")
    assert second["added_daily_rows"] == 0
    store.db.close()


def test_apply_rejects_tampered_plan_before_writing(tmp_path):
    store = DataStore(tmp_path / "data")
    repo = KlineRepository(store)
    make_plan(repo, tmp_path / "plan")
    daily(close=10.5).write_parquet(tmp_path / "plan/daily.parquet")
    with pytest.raises(ValueError, match="Plan changed"):
        repair.apply_repair(repo, tmp_path / "plan", tmp_path / "apply")
    assert not list((tmp_path / "data/kline_daily").rglob("*.parquet"))
    store.db.close()


def test_prepare_empty_symbol_fails_closed(tmp_path):
    store = DataStore(tmp_path / "data")
    provider = SimpleNamespace(iter_daily=lambda *a, **kw: iter([]))
    with pytest.raises(ValueError, match="No rows"):
        repair.prepare_repair(
            KlineRepository(store),
            provider,
            ranges(),
            tmp_path / "plan",
            factor_end=date(2026, 9, 30),
        )
    store.db.close()


def test_plan_artifacts_cannot_pollute_market_parquet_tree(tmp_path):
    store = DataStore(tmp_path / "data")
    with pytest.raises(ValueError, match="under research"):
        make_plan(KlineRepository(store), tmp_path / "data/kline_daily/repair")
    assert not list((tmp_path / "data/kline_daily").rglob("*.parquet"))
    store.db.close()


def test_apply_recomputes_full_selected_history_and_preserves_other_enriched(tmp_path):
    from polars.testing import assert_frame_equal

    from app.enriched_generation import get_enriched_generation
    from app.indicators import pipeline
    from app.parquet import scan_daily_parquet

    root = tmp_path / "data"
    store = DataStore(root)
    repo = KlineRepository(store)
    early, late = date(2022, 8, 18), date(2025, 9, 10)
    target, other = "001222.SZ", "600000.SH"
    try:
        repo.append_daily(
            pl.concat(
                [
                    daily(target, late, 10.5),
                    daily(other, early, 10.2),
                    daily(other, late, 10.8),
                ]
            )
        )
        factor_dir = root / "adj_factor"
        factor_dir.mkdir(exist_ok=True)
        old_factor = pl.DataFrame(
            {
                "symbol": [other],
                "trade_date": [date(2023, 6, 27)],
                "ex_factor": [1.1],
            }
        )
        old_factor.write_parquet(factor_dir / "all.parquet")
        pipeline.run_pipeline(root)
        original = pl.read_parquet(str(root / "kline_daily_enriched/**/*.parquet"))
        original_other = original.filter(pl.col("symbol") == other).sort("date")
        generation_before = get_enriched_generation(root)
        provider = SimpleNamespace(
            name="fixture",
            iter_daily=lambda *a, **kw: iter([daily(target, early, 20.0)]),
            get_adj_factors=lambda *a, **kw: pl.DataFrame(
                {
                    "symbol": [target],
                    "trade_date": [date(2023, 6, 27)],
                    "ex_factor": [2.0],
                }
            ),
        )
        repair.prepare_repair(
            repo, provider, ranges(), tmp_path / "plan", factor_end=date(2026, 9, 30)
        )
        result = repair.apply_repair(repo, tmp_path / "plan", tmp_path / "apply")
        assert result["status"] == "complete"
        assert result["added_daily_rows"] == 1
        assert result["added_factor_events"] == 1
        assert result["existing_values_preserved"] is True

        actual = pl.read_parquet(str(root / "kline_daily_enriched/**/*.parquet"))
        actual_target = actual.filter(pl.col("symbol") == target).sort("date")
        assert actual_target["date"].to_list() == [early, late]
        assert actual_target["raw_close"].to_list() == [20.0, 10.5]
        assert actual_target["close"].to_list() == [10.0, 10.5]
        assert_frame_equal(actual.filter(pl.col("symbol") == other).sort("date"), original_other)
        assert get_enriched_generation(root) != generation_before

        raw_target = (
            scan_daily_parquet(str(root / "kline_daily/**/*.parquet"))
            .filter(pl.col("symbol") == target)
            .sort("date")
            .collect()
        )
        factors = pl.read_parquet(factor_dir / "all.parquet")
        expected = pipeline._select_storage_cols(
            pipeline.compute_enriched(
                raw_target,
                factors=factors.filter(pl.col("symbol") == target),
            )
        ).sort("date")
        assert_frame_equal(actual_target.select(expected.columns), expected)
        assert_frame_equal(factors.filter(pl.col("symbol") == other), old_factor)
    finally:
        store.db.close()


def test_retry_completes_enriched_after_raw_write_succeeded(tmp_path, monkeypatch):
    from app.indicators.pipeline import run_pipeline
    from app.parquet import scan_enriched_parquet

    store = DataStore(tmp_path / "data")
    repo = KlineRepository(store)
    try:
        repo.append_daily(daily(day=date(2025, 9, 10)))
        run_pipeline(store.data_dir)
        make_plan(repo, tmp_path / "plan")
        actual_recompute = repair.recompute
        calls = []

        def fail_once(repo, symbols):
            calls.append(list(symbols))
            if len(calls) == 1:
                raise RuntimeError("enriched calculation failed before first write")
            actual_recompute(repo, symbols)

        monkeypatch.setattr(repair, "recompute", fail_once)
        with pytest.raises(RuntimeError, match="before first write"):
            repair.apply_repair(repo, tmp_path / "plan", tmp_path / "failed")
        assert not (store.data_dir / "kline_daily_enriched/date=2022-08-18/part.parquet").exists()
        result = repair.apply_repair(repo, tmp_path / "plan", tmp_path / "retry")
        assert result["status"] == "complete"
        assert result["added_daily_rows"] == 0
        assert len(calls) == 2
        enriched = scan_enriched_parquet(
            str(store.data_dir / "kline_daily_enriched/**/*.parquet")
        ).collect()
        assert enriched.sort("date")["date"].to_list() == [date(2022, 8, 18), date(2025, 9, 10)]
    finally:
        store.db.close()
