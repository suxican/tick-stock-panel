"""Persisted Huichun observations use trading sessions and causal raw-price returns."""

import json
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services import huichun_mode as module
from app.services.huichun_mode import HuichunModeService
from app.tickflow.repository import DataStore, KlineRepository


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, default=str), encoding="utf-8")


@pytest.fixture
def observations(tmp_path, monkeypatch):
    # The synthetic closure on Tuesday and the weekend must not consume horizons.
    days = [date(2026, 10, day) for day in (12, 14, 15, 16, 19, 20)]
    prices = [10.0, 10.5, 5.5, 5.4, 5.6, 5.7]
    symbol = "600001.SH"
    store = DataStore(tmp_path / "data")
    raw = pl.DataFrame([
        {
            "symbol": symbol, "date": day, "open": price, "high": price * 1.01,
            "low": price * 0.99, "close": price, "volume": 10000.0,
            "amount": price * 10000.0,
        }
        for day, price in zip(days, prices, strict=True)
    ])
    # A pre-signal adjustment changes the price scale, but cancels from returns.
    factors = pl.DataFrame({
        "symbol": [symbol, symbol], "trade_date": [days[0], days[2]],
        "ex_factor": [2.0, 2.0],
    })
    raw_path = store.data_dir / "kline_daily/fixture.parquet"
    raw.write_parquet(raw_path)
    factors.write_parquet(store.data_dir / "adj_factor/fixture.parquet")
    store._register_views()
    calendar_path = tmp_path / "calendar.json"
    write_json(calendar_path, {
        "version": 1, "coverage_start": "2026-10-01", "coverage_end": "2026-12-31",
        "weekends_closed": True,
        "sources": [{
            "id": "fixture", "url": "https://www.sse.com.cn/notice",
            "published_on": "2026-01-01", "title": "Synthetic test calendar",
        }],
        "closures": [{
            "start_date": "2026-10-13", "end_date": "2026-10-13", "source_id": "fixture",
        }],
    })
    monkeypatch.setattr(module, "_now", lambda: datetime(2026, 10, 20, 16, tzinfo=CN_TZ))
    repo = KlineRepository(store)
    monkeypatch.setattr(repo, "get_enriched_latest", lambda: (
        pl.DataFrame({"symbol": [symbol], "date": [days[-1]], "raw_close": [5.7]}), None,
    ))
    service = HuichunModeService(repo, calendar_path=calendar_path)
    scan_id = "a" * 32
    candidate_id = "signal-fixture"
    write_json(service.root / f"scan-{scan_id}.json", {
        "id": scan_id, "rule_revision": 0, "rules": service.get_config()["rules"],
        "candidates": [{
            "id": candidate_id, "symbol": symbol, "name": "测试股票",
            "signal_date": str(days[0]), "raw_close": 10.0, "adjusted_close": 5.0,
            "baseline_fingerprint": service._baseline(raw, factors, symbol, days[0]),
        }],
    })
    yield SimpleNamespace(
        service=service, repo=repo, store=store, days=days, raw=raw, raw_path=raw_path,
        calendar_path=calendar_path, scan_id=scan_id, candidate_id=candidate_id,
    )
    service.close(timeout=10)
    store.db.close()


def add_observation(env, service=None):
    service = service or env.service
    service.add_tracking(env.scan_id, [env.candidate_id])
    result = service.wait(timeout=10)
    assert result["job"]["status"] == "completed", result["job"]
    return service.get_tracking()["records"][0]


def refresh(service):
    service.start_tracking_refresh()
    result = service.wait(timeout=10)
    assert result["job"]["status"] == "completed", result["job"]
    return service.get_tracking()["records"][0]


def test_returns_use_raw_baseline_post_signal_factors_and_five_trading_sessions(observations):
    env = observations
    record = add_observation(env)
    assert record["base_close"] == 10.0
    assert record["base_adjusted_close"] == 5.0
    assert [item["target_date"] for item in record["returns"]] == [
        str(day) for day in env.days[1:]
    ]
    assert [item["status"] for item in record["returns"]] == ["ok"] * 5
    assert [item["return_pct"] for item in record["returns"]] == pytest.approx(
        [0.05, 0.10, 0.08, 0.12, 0.14]
    )
    assert record["current_basis"] == {
        "quote_date": "2026-10-20", "status": "ok", "factor_multiplier": 2.0,
        "factor_status": "events_present_unverified",
    }
    disk = json.loads((env.service.root / "tracking.json").read_text(encoding="utf-8"))
    assert disk["records"] == [record]


def test_missing_session_does_not_shift_horizons_or_invent_zero_returns(observations):
    env = observations
    add_observation(env)
    env.raw.filter(pl.col("date") != env.days[3]).write_parquet(env.raw_path)
    record = refresh(env.service)
    assert [item["target_date"] for item in record["returns"]] == [
        str(day) for day in env.days[1:]
    ]
    assert [item["status"] for item in record["returns"]] == [
        "ok", "ok", "unknown_gap", "unknown_gap", "unknown_gap",
    ]
    assert all(item["return_pct"] is None for item in record["returns"][2:])
    assert record["current_basis"]["status"] == "unknown_gap"


def test_factor_completeness_is_separate_from_computable_returns(observations):
    record = add_observation(observations)
    assert record["returns"][0]["status"] == "ok"
    assert record["returns"][0]["factor_status"] == "no_events_unverified"
    assert record["returns"][1]["status"] == "ok"
    assert record["returns"][1]["factor_status"] == "events_present_unverified"
    assert record["current_basis"]["factor_status"] == "events_present_unverified"


def test_empty_factors_do_not_certify_price_return(observations):
    env = observations
    factor_path = env.store.data_dir / "adj_factor/fixture.parquet"
    pl.read_parquet(factor_path).head(0).write_parquet(factor_path)
    scan_path = env.service.root / f"scan-{env.scan_id}.json"
    scan = json.loads(scan_path.read_text(encoding="utf-8"))
    scan["candidates"][0]["baseline_fingerprint"] = env.service._baseline(
        env.raw, pl.DataFrame(schema=module.FACTOR_SCHEMA), "600001.SH", env.days[0],
    )
    write_json(scan_path, scan)
    record = add_observation(env)
    assert record["returns"][1]["status"] == "ok"
    assert record["returns"][1]["return_pct"] == pytest.approx(-0.45)
    assert all(item["factor_status"] == "no_events_unverified" for item in record["returns"])


def test_legacy_factor_status_is_unknown_without_rewriting_saved_records(observations):
    env = observations
    record = add_observation(env)
    for item in record["returns"]:
        item.pop("factor_status", None)
    record["current_basis"].pop("factor_status", None)
    path = env.service.root / "tracking.json"
    write_json(path, {"records": [record], "updated_at": None})
    original = path.read_bytes()
    loaded = env.service.get_tracking()["records"][0]
    assert all(item["factor_status"] == "unknown" for item in loaded["returns"])
    assert loaded["current_basis"]["factor_status"] == "unknown"
    assert path.read_bytes() == original


def test_tracking_freezes_cycle_dates_from_selected_scan(observations):
    env = observations
    scan_path = env.service.root / f"scan-{env.scan_id}.json"
    saved_scan = json.loads(scan_path.read_text(encoding="utf-8"))
    saved_scan["candidates"][0].update(g_date="2026-08-03", d_date="2026-09-10")
    write_json(scan_path, saved_scan)
    record = add_observation(env)
    assert (record["g_date"], record["d_date"]) == ("2026-08-03", "2026-09-10")
    saved_scan["candidates"][0].update(g_date="2026-08-04", d_date="2026-09-11")
    write_json(scan_path, saved_scan)
    refreshed = refresh(env.service)
    assert (refreshed["g_date"], refreshed["d_date"]) == ("2026-08-03", "2026-09-10")


def test_later_invalid_factor_does_not_change_prior_horizon_status(observations):
    env = observations
    original = add_observation(env)
    factor_path = env.store.data_dir / "adj_factor/fixture.parquet"
    factors = pl.read_parquet(factor_path)
    factors.with_columns(
        pl.when(pl.col("trade_date") == env.days[2])
        .then(10.0).otherwise(pl.col("ex_factor")).alias("ex_factor")
    ).write_parquet(factor_path)
    record = refresh(env.service)
    assert record["returns"][0] == original["returns"][0]
    assert all(item["status"] == "invalid_factor" for item in record["returns"][1:])
    assert all(item["factor_status"] == "invalid" for item in record["returns"][1:])
    assert record["current_basis"]["factor_status"] == "invalid"


def test_missing_calendar_withholds_all_returns_but_keeps_the_record(observations, tmp_path):
    env = observations
    service = HuichunModeService(env.repo, calendar_path=tmp_path / "missing-calendar.json")
    try:
        record = add_observation(env, service)
        assert record["id"] == env.candidate_id
        assert [item["status"] for item in record["returns"]] == ["unknown_gap"] * 5
        assert all(item["return_pct"] is None for item in record["returns"])
        assert record["current_basis"]["status"] == "unknown_gap"
    finally:
        service.close(timeout=10)


def test_changed_signal_price_invalidates_returns_without_rewriting_baseline(observations):
    env = observations
    original = add_observation(env)
    env.raw.with_columns(
        pl.when(pl.col("date") == env.days[0]).then(10.01).otherwise(pl.col("close")).alias("close")
    ).write_parquet(env.raw_path)
    record = refresh(env.service)
    assert record["base_close"] == original["base_close"]
    assert record["baseline_fingerprint"] == original["baseline_fingerprint"]
    assert [item["status"] for item in record["returns"]] == ["baseline_changed"] * 5
    assert all(item["return_pct"] is None for item in record["returns"])
    assert record["current_basis"]["status"] == "baseline_changed"


def test_future_observations_remain_pending(observations, monkeypatch):
    monkeypatch.setattr(module, "_now", lambda: datetime(2026, 10, 14, 16, tzinfo=CN_TZ))
    record = add_observation(observations)
    assert record["returns"][0]["return_pct"] == pytest.approx(0.05)
    assert [item["status"] for item in record["returns"]] == ["ok", *(["pending"] * 4)]
    assert all(item["target_date"] is None for item in record["returns"][1:])
    assert all(item["return_pct"] is None for item in record["returns"][1:])


def test_legacy_record_without_current_basis_survives_restart_and_refresh(observations):
    env = observations
    original = add_observation(env)
    original.pop("current_basis")
    write_json(env.service.root / "tracking.json", {"records": [original], "updated_at": None})
    restarted = HuichunModeService(env.repo, calendar_path=env.calendar_path)
    try:
        assert restarted.get_tracking()["records"] == [original]
        updated = refresh(restarted)
        assert updated["id"] == original["id"]
        assert updated["added_at"] == original["added_at"]
        assert updated["base_adjusted_close"] == original["base_adjusted_close"]
        assert updated["returns"] == original["returns"]
        assert updated["current_basis"]["status"] == "ok"
    finally:
        restarted.close(timeout=10)


def test_config_revision_is_shared_persisted_and_rejects_stale_writes(observations):
    env = observations
    second = HuichunModeService(env.repo, calendar_path=env.calendar_path)
    try:
        saved = env.service.save_config({"rally_threshold": 0.5}, expected_revision=0)
        assert saved["revision"] == 1
        assert second.get_config() == saved
        with pytest.raises(ValueError, match="版本已变化"):
            second.save_config({"rally_threshold": 0.6}, expected_revision=0)
        assert second.save_config(saved["rules"], expected_revision=1) == saved
        persisted = json.loads((env.service.root / "config.json").read_text(encoding="utf-8"))
        assert [version["revision"] for version in persisted["versions"]] == [0, 1]
        assert persisted["current"] == saved
    finally:
        second.close(timeout=10)


def test_restart_marks_interrupted_job_failed_and_preserves_previous_scan(observations):
    env = observations
    previous_scan = {"id": "completed-scan", "candidates": [], "rule_revision": 0}
    state = env.service.get_snapshot()
    state.pop("config_revision")
    state.update(scan=previous_scan)
    state["job"].update(status="running", kind="scan", id="interrupted")
    write_json(env.service.root / "snapshot.json", state)
    restarted = HuichunModeService(env.repo, calendar_path=env.calendar_path)
    try:
        snapshot = restarted.get_snapshot()
        assert snapshot["scan"] == previous_scan
        assert snapshot["job"]["status"] == "failed"
        assert "重启中断" in snapshot["job"]["error"]
        restarted.start_tracking_refresh()
        assert restarted.wait(timeout=10)["job"]["status"] == "completed"
        persisted = json.loads((env.service.root / "snapshot.json").read_text(encoding="utf-8"))
        assert persisted["job"]["status"] == "completed"
        assert persisted["scan"] == previous_scan
    finally:
        restarted.close(timeout=10)


def test_removing_tracking_is_persisted_and_readding_preserves_observation_history(observations):
    env = observations
    original = add_observation(env)
    assert env.service.remove_tracking(env.candidate_id)["records"] == []
    saved = json.loads((env.service.root / "tracking.json").read_text(encoding="utf-8"))
    assert saved["records"][0]["removed_at"]
    restored = add_observation(env)
    assert restored["previous_observations"][0]["returns"] == original["returns"]
    assert restored["previous_observations"][0]["removed_at"]

