import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.huichun import router
from app.market_time import CN_TZ
from app.services import huichun_mode as module
from app.services.huichun_calendar import BUNDLED_CALENDAR_PATH
from app.services.huichun_mode import HuichunModeService
from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture
def watch_environment(tmp_path, monkeypatch):
    values = (
        [10.0] * 260
        + [10 - i * 0.1 for i in range(1, 21)]
        + [8 + i * 0.4 for i in range(1, 36)]
        + [22 - i * 0.1 for i in range(1, 31)]
        + [19 + i * 0.1 for i in range(1, 61)]
    )
    first = date(2022, 1, 3)
    days = [
        first + timedelta(days=i) for i in range(650) if (first + timedelta(days=i)).weekday() < 5
    ][: len(values)]
    store = DataStore(tmp_path / "data")
    raw = pl.DataFrame(
        [
            {
                "symbol": symbol,
                "date": day,
                "open": value,
                "high": value * 1.01,
                "low": value * 0.99,
                "close": value,
                "volume": 10000.0,
                "amount": value * 1000000.0,
            }
            for symbol, prices in [("600001.SH", values), ("600002.SH", [10.0] * len(days))]
            for day, value in zip(days, prices, strict=True)
        ]
    )
    raw.write_parquet(store.data_dir / "kline_daily/test.parquet")
    factors = pl.DataFrame({"symbol": ["600001.SH"], "trade_date": [days[0]], "ex_factor": [1.0]})
    factors.write_parquet(store.data_dir / "adj_factor/test.parquet")
    pl.DataFrame(
        {
            "symbol": ["600001.SH", "600002.SH"],
            "name": ["候选", "平盘"],
            "listing_date": [str(days[0])] * 2,
        }
    ).write_parquet(store.data_dir / "instruments/test.parquet")
    store._register_views()
    calendar_path = tmp_path / "calendar.json"
    calendar_path.write_text(
        json.dumps(
            {
                "version": 1,
                "coverage_start": "2022-01-01",
                "coverage_end": "2026-12-31",
                "weekends_closed": True,
                "closures": [],
                "sources": [
                    {
                        "id": "synthetic",
                        "url": "https://www.sse.com.cn/notice",
                        "title": "Fixture",
                        "published_on": "2021-12-01",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        module,
        "_now",
        lambda: datetime.combine(days[-1], datetime.min.time(), CN_TZ).replace(hour=16),
    )
    repo = KlineRepository(store)
    monkeypatch.setattr(repo, "get_enriched_latest", lambda: (pl.DataFrame(), None))
    svc = HuichunModeService(repo, calendar_path=calendar_path)
    svc.save_config({"ma_window": 3, "slope_lag": 1, "zero_threshold": 1.0}, expected_revision=0)
    yield svc, raw, factors, days, calendar_path
    svc.close(timeout=10)
    store.db.close()


def scan(svc, days):
    svc.start_scan(start_date=days[0], end_date=days[-1])
    result = svc.wait()
    assert result["job"]["status"] == "completed", result["job"]
    return result["scan"]


def replace_raw(svc, raw):
    raw.write_parquet(svc.repo.store.data_dir / "kline_daily/test.parquet")


def replace_factors(svc, factors):
    factors.write_parquet(svc.repo.store.data_dir / "adj_factor/test.parquet")


@pytest.mark.parametrize("when,day_offset,expected", [
    ("scan", 1, "completed"), ("scan", 0, "failed"),
    ("tracking_read", 1, "failed"), ("after_scan", 0, "failed"),
    ("research_read", 1, "failed"),
])
def test_history_scan_guards_each_read_window_and_handoff(watch_environment, monkeypatch, when, day_offset, expected):
    svc, raw, _, days, _ = watch_environment
    end = days[300]
    path = svc.repo.store.data_dir / "kline_daily" / f"date={days[300 + day_offset]}" / "extra.parquet"
    def mutate():
        path.parent.mkdir(parents=True, exist_ok=True)
        raw.head(0).write_parquet(path)
    original = svc._scan
    def run_scan(*args):
        result = original(*args)
        if when == "scan":
            mutate()
        return result
    monkeypatch.setattr(svc, "_scan", run_scan)
    def tracking():
        if when in ("after_scan", "tracking_read"):
            mutate()
        return {"tracked": {}} if when == "tracking_read" else {}
    monkeypatch.setattr(svc, "_calculate_tracking", tracking)
    if when == "research_read":
        def refresh(*args):
            mutate()
            return {"records": [], "updated_at": None}
        monkeypatch.setattr(module.huichun_research, "refresh", refresh)
    svc.start_scan(end, end)
    state = svc.wait()
    assert state["job"]["status"] == expected, state["job"]
    if expected == "failed":
        assert "行情已更新" in state["job"]["error"]


def test_cross_threshold_and_first_watch_are_aligned_with_the_next_real_bar(watch_environment):
    svc, raw, factors, days, _ = watch_environment
    replace_factors(svc, factors.with_columns(pl.lit(2.0).alias("ex_factor")))
    seen = scan(svc, days[:349])["observations"][0]
    threshold = seen["cross_threshold_price"]
    assert threshold == pytest.approx(seen["raw_close"] * (1 + seen["cross_threshold_return"]))
    first = date.fromisoformat(seen["first_watch_date"])
    later = scan(svc, days[:350])
    row = later["candidates"][0]
    lead = days.index(days[349]) - days.index(first)
    assert row["first_watch_date"] == str(first)
    assert row["watch_lead_bars"] == lead
    for horizon in later["watch_conversion"]:
        assert horizon["total"] == horizon["resolved"] == 1
        assert horizon["confirmed"] == int(lead <= horizon["horizon"])
    assert seen["threshold_zero_pass"] is True
    assert isinstance(seen["threshold_trend_pass"], bool)
    for scale, has_cross in [(0.99999, False), (1.00001, True)]:
        price = threshold * scale
        changed = raw.with_columns([
            pl.when((pl.col("symbol") == seen["symbol"]) & (pl.col("date") == days[349]))
            .then(pl.lit(price)).otherwise(pl.col(key)).alias(key)
            for key in ("open", "high", "low", "close")
        ])
        prepared, _ = svc._prepare_scan_batch(
            changed, factors.with_columns(pl.lit(2.0).alias("ex_factor")), calendar=days[:350],
            end=days[349], listings={}, expected_symbols=[seen["symbol"]],
        )
        result = module.audit_a0(
            prepared.frame, start=days[349], end=days[349],
            params=module.A0Params(ma_window=3, slope_lag=1, zero_threshold=1.0),
        )
        bar = result.indicators.filter(pl.col("symbol") == seen["symbol"]).row(0, named=True)
        assert bar["golden_cross"] is has_cross


def at_close(monkeypatch, day):
    monkeypatch.setattr(module, "_now", lambda: datetime.combine(
        day, datetime.min.time(), CN_TZ,
    ).replace(hour=16))


@pytest.mark.parametrize("kind,status", [
    ("stale", "stale"), ("snapshot", "incomplete"), ("partial", "incomplete"),
    ("unverified", "calendar_unverified"), ("history", "historical"), ("raw_issue", "incomplete"),
    ("before_final_cutoff", "incomplete"),
])
def test_monitor_requires_current_final_daily_batch(watch_environment, monkeypatch, kind, status):
    svc, raw, _, days, _ = watch_environment
    cutoff = days[349]
    at_close(monkeypatch, days[350] if kind in {"stale", "history"} else cutoff)
    if kind == "before_final_cutoff":
        monkeypatch.setattr(module, "_now", lambda: datetime.combine(
            cutoff, datetime.min.time(), CN_TZ,
        ).replace(hour=15, minute=25))
    raw = raw.filter(pl.col("date") <= (days[350] if kind == "history" else cutoff))
    if kind == "snapshot":
        quote_ms = int(datetime.combine(cutoff, datetime.min.time(), CN_TZ)
                       .replace(hour=15, minute=1).timestamp() * 1000)
        raw = raw.with_columns(pl.when(pl.col("date") == cutoff)
                               .then(quote_ms).otherwise(None).alias("quote_ts"))
    if kind == "partial":
        raw = raw.filter((pl.col("date") != cutoff) | (pl.col("symbol") == "600001.SH"))
    if kind == "unverified":
        monkeypatch.setattr(svc, "_calendar_path", None)
    if kind == "raw_issue":
        from app.services import daily_health
        from app.services.data_integrity import IntegrityIssue

        daily_health.record_issues(svc.repo.store.data_dir, [
            IntegrityIssue(cutoff, "kline_daily", "invalid", symbols=("600002.SH",)),
        ])
    replace_raw(svc, raw)
    calls = []
    svc._publish = calls.extend
    result = scan(svc, days[:350])
    assert result["candidates"]  # Results stay available for research.
    assert result["coverage"]["publication_status"] == status
    assert result["coverage"]["expected_daily_date"] == (
        None if kind == "unverified" else str(module._now().date())
    )
    checks = result["coverage"]["publication_checks"]
    assert checks["active_symbols"] == (1 if kind == "partial" else 2)
    assert checks["previous_active_symbols"] == 2
    assert checks["previous_date"] == str(days[348])
    assert checks["unfinalized_symbols"] == (2 if kind in {"snapshot", "before_final_cutoff"} else 0)
    assert checks["known_raw_issue"] is (kind == "raw_issue")
    assert calls == []


def test_at_final_cutoff_snapshot_can_notify_over_the_weekend(watch_environment, monkeypatch):
    svc, raw, _, days, _ = watch_environment
    friday = days[349]
    assert friday.weekday() == 4
    at_close(monkeypatch, friday + timedelta(days=2))
    ts = int(datetime.combine(friday, datetime.min.time(), CN_TZ)
             .replace(hour=15, minute=30).timestamp() * 1000)
    raw = raw.filter(pl.col("date") <= friday).with_columns(
        pl.when(pl.col("date") == friday).then(ts).otherwise(None).alias("quote_ts"),
    )
    replace_raw(svc, raw)
    calls = []
    svc._publish = calls.extend
    result = scan(svc, days[:350])
    assert result["coverage"]["publication_status"] == "ready"
    assert result["coverage"]["expected_daily_date"] == str(friday)
    assert [e["date"] for e in calls] == [str(friday)]


def test_mid_scan_session_close_suppresses_old_day_publication(watch_environment, monkeypatch):
    svc, raw, _, days, _ = watch_environment
    day = days[349]
    clock = [datetime.combine(day, datetime.min.time(), CN_TZ).replace(hour=14)]
    monkeypatch.setattr(module, "_now", lambda: clock[0])
    replace_raw(svc, raw.filter(pl.col("date") <= days[348]))
    original = svc._publish_scan_events

    def after_next_close(scan):
        clock[0] = clock[0].replace(hour=15)
        original(scan)

    monkeypatch.setattr(svc, "_publish_scan_events", after_next_close)
    calls = []
    svc._publish = calls.extend
    result = scan(svc, days[:349])
    assert result["coverage"]["publication_status"] == "ready"
    assert result["observations"] and calls == []


def test_stage_counts_report_rejected_first_cross(watch_environment):
    svc, _, _, days, _ = watch_environment
    config = svc.get_config()
    svc.save_config({**config["rules"], "zero_threshold": 0.0}, expected_revision=config["revision"])
    coverage = scan(svc, days[:350])["coverage"]
    stages = coverage["stage_counts"]
    assert stages["first_cross_today"] == stages["first_cross_rejected_today"] == 1
    assert stages["confirmed_today"] == stages["pending_today"] == 0
    assert coverage["first_cross_rejection_reasons"] == {
        "zero_distance_exceeded": 1, "close_not_above_ma": 0, "ma_not_rising": 0,
        "indicator_history_insufficient": 0,
    }


def test_cutoff_rejections_can_have_overlapping_reasons():
    from app.services.huichun_coverage import stage_counts
    from app.strategy.huichun_a0 import A0AuditResult

    day = date(2026, 11, 4)
    cycles = pl.DataFrame({
        "rally_qualified": [True, True, True], "r_date": [day, day, date(2026, 11, 3)],
        "status": ["first_cross_rejected"] * 3, "last_observation_date": [day] * 3,
        "shape_match": [False] * 3,
        "reasons": ["zero_distance_exceeded;ma_not_rising", "ma_not_rising", "close_not_above_ma"],
    })
    prices = pl.DataFrame({"date": [day] * 2, "prior_bars": [260] * 2})
    counts, reasons = stage_counts(A0AuditResult(cycles, pl.DataFrame(), prices), day, 250)
    assert counts["first_cross_rejected_today"] == 2
    assert reasons == {"zero_distance_exceeded": 1, "ma_not_rising": 2}


@pytest.mark.parametrize("active,expected", [(94, "incomplete"), (95, "ready")])
def test_publication_coverage_boundary(active, expected):
    from collections import Counter

    from app.services.huichun_coverage import publication_status

    day = date(2026, 11, 4)
    counts = Counter(active_symbols=active, previous_active_symbols=100)
    assert publication_status(day, day, day, counts, False) == expected


@pytest.mark.parametrize("index,observed,confirmed", [(348, 1, 0), (349, 0, 1)])
def test_stage_counts_describe_cutoff_even_for_range_scans(watch_environment, index, observed, confirmed):
    svc, _, _, days, _ = watch_environment
    cov = scan(svc, days[:index + 1])["coverage"]
    assert cov["stage_counts"] == {
        "universe": 2, "valid_close": 2, "warmup_ready": 2,
        "qualified_waiting_first_cross": observed, "first_cross_today": confirmed,
        "first_cross_rejected_today": 0, "confirmed_today": confirmed, "pending_today": observed,
    }


def test_factor_issue_dates_count_unique_symbols(watch_environment):
    svc, _, _, days, _ = watch_environment
    replace_factors(svc, pl.DataFrame({
        "symbol": ["600001.SH", "600001.SH", "600002.SH"],
        "trade_date": [days[340], days[342], days[340]], "ex_factor": [-1.0] * 3,
    }))
    cov = scan(svc, days)["coverage"]
    assert cov["factor_issue_dates"] == [{"date": str(days[340]), "count": 2}]
    assert cov["stage_counts"]["valid_close"] == 0


@pytest.mark.parametrize("factor_values", [[10.0], [None], [1.0, 1.0]])
def test_later_invalid_factor_preserves_earlier_candidates(watch_environment, factor_values):
    svc, _, factors, days, _ = watch_environment
    before = scan(svc, days[:381])
    assert len(before["candidates"]) == 1
    event_day = days[390]
    later = pl.DataFrame({
        "symbol": ["600001.SH"] * len(factor_values),
        "trade_date": [event_day] * len(factor_values),
        "ex_factor": pl.Series(factor_values, dtype=pl.Float64),
    })
    replace_factors(svc, pl.concat([factors, later]))
    after = scan(svc, days)
    assert after["candidates"] == before["candidates"]
    assert after["observations"] == []
    assert after["coverage"]["factor_excluded_count"] == 0
    assert after["coverage"]["factor_quarantined_count"] == 1
    assert after["coverage"]["factor_issues"] == [
        {"symbol": "600001.SH", "invalid_from": str(event_day)},
    ]


def test_undated_factor_fails_closed_with_explicit_coverage(watch_environment):
    svc, _, factors, days, _ = watch_environment
    replace_factors(svc, factors.with_columns(pl.lit(None, dtype=pl.Date).alias("trade_date")))
    result = scan(svc, days)
    assert result["candidates"] == []
    assert result["coverage"]["factor_excluded_count"] == 1
    assert result["coverage"]["factor_issues"] == [
        {"symbol": "600001.SH", "invalid_from": None},
    ]


def test_current_st_name_does_not_rewrite_historical_candidates(watch_environment):
    svc, _, _, days, _ = watch_environment
    before = scan(svc, days[:381])
    instruments = svc.repo.store.data_dir / "instruments/test.parquet"
    pl.read_parquet(instruments).with_columns(
        pl.when(pl.col("symbol") == "600001.SH")
        .then(pl.lit("*ST 候选")).otherwise(pl.col("name")).alias("name")
    ).write_parquet(instruments)
    after = scan(svc, days[:381])
    assert [r["id"] for r in after["candidates"]] == [r["id"] for r in before["candidates"]]
    candidate = after["candidates"][0]
    assert candidate["eligibility_status"] == "unknown"
    assert candidate["current_risk_warning"] is True
    assert candidate["factor_status"] == "events_present_unverified"
    assert after["coverage"]["st_excluded_count"] == 0
    assert after["coverage"]["st_filter"] == "not_applied_historical_status_unknown"
    assert after["data_policy_version"] == 2


@pytest.mark.parametrize("cutoff_index,event_type,pool", [
    (348, "pending_cross", "observations"),
    (349, "a0_confirmed", "candidates"),
])
def test_latest_scan_publishes_dated_raw_price_events_after_persistence(
    watch_environment, monkeypatch, cutoff_index, event_type, pool,
):
    svc, raw, factors, days, calendar_path = watch_environment
    cutoff = days[cutoff_index]
    at_close(monkeypatch, cutoff)
    replace_raw(svc, raw.filter(pl.col("date") <= cutoff))
    replace_factors(svc, factors.with_columns(pl.lit(2.0).alias("ex_factor")))
    published = []

    def publish(events):
        state = json.loads((svc.root / "snapshot.json").read_text(encoding="utf-8"))
        assert state["job"]["status"] == "completed"
        assert state["scan"]["observation_date"] == str(cutoff)
        published.extend(events)

    monitored = HuichunModeService(svc.repo, calendar_path=calendar_path, publish=publish)
    try:
        result = scan(monitored, days[:cutoff_index + 1])
        row = result[pool][0]
        assert row["adjusted_close"] == pytest.approx(row["raw_close"] * 2)
        assert len(published) == 1
        event = published[0]
        assert event["source"] == "huichun"
        assert event["type"] == event["event_type"] == event_type
        assert event["date"] == str(cutoff)
        assert event["ts"] == int(module._now().timestamp() * 1000)
        assert event["symbol"] == row["symbol"] and event["name"] == row["name"]
        assert event["price"] == row["raw_close"]
        assert event["rule_revision"] == result["rule_revision"]
        assert "日线观察" in event["message"] and "不代表成交" in event["message"]
        assert str(cutoff) in event["message"]
        if event_type == "pending_cross":
            assert "尚未确认金叉" in event["message"]
        assert event.get("change_pct") is None
        scan(monitored, days[:cutoff_index + 1])
        assert len(published) == 2 and published[1]["id"] == event["id"]
        monitored.start_tracking_refresh()
        assert monitored.wait()["job"]["status"] == "completed"
        assert len(published) == 2
    finally:
        monitored.close(timeout=10)


@pytest.mark.parametrize("cutoff_index", [348, 349, 404])
def test_history_and_old_candidates_do_not_publish_as_latest_events(
    watch_environment, cutoff_index,
):
    svc, _, _, days, calendar_path = watch_environment
    published = []
    monitored = HuichunModeService(
        svc.repo, calendar_path=calendar_path, publish=published.extend,
    )
    try:
        result = scan(monitored, days[:cutoff_index + 1])
        assert result["candidates"] or result["observations"]
        assert published == []
    finally:
        monitored.close(timeout=10)


def test_intraday_scan_publishes_only_last_completed_day(watch_environment, monkeypatch):
    svc, _, _, days, calendar_path = watch_environment
    monkeypatch.setattr(module, "_now", lambda: datetime.combine(
        days[349], datetime.min.time(), CN_TZ,
    ).replace(hour=10))
    published = []
    monitored = HuichunModeService(
        svc.repo, calendar_path=calendar_path, publish=published.extend,
    )
    try:
        monitored.start_scan()
        state = monitored.wait()
        assert state["job"]["status"] == "completed"
        assert len(published) == 1
        assert published[0]["event_type"] == "pending_cross"
        assert published[0]["date"] == str(days[348])
    finally:
        monitored.close(timeout=10)


@pytest.mark.parametrize("failure", ["tracking", "snapshot", "revision"])
def test_failed_or_superseded_scan_never_publishes_events(
    watch_environment, monkeypatch, failure,
):
    svc, raw, _, days, calendar_path = watch_environment
    at_close(monkeypatch, days[349])
    replace_raw(svc, raw.filter(pl.col("date") <= days[349]))
    published = []
    monitored = HuichunModeService(
        svc.repo, calendar_path=calendar_path, publish=published.extend,
    )
    if failure == "snapshot":
        write = monitored._write

        def fail_write(name, value):
            if name == "snapshot.json" and value["job"]["status"] == "completed":
                raise OSError("disk full")
            write(name, value)

        monkeypatch.setattr(monitored, "_write", fail_write)
    else:
        def calculate_tracking():
            if failure == "tracking":
                raise ValueError("tracking failed")
            monitored.save_config({"rally_threshold": 5.0}, expected_revision=1)
            return {}

        monkeypatch.setattr(monitored, "_calculate_tracking", calculate_tracking)
    try:
        monitored.start_scan(days[0], days[349])
        state = monitored.wait()
        assert state["job"]["status"] == ("completed" if failure == "revision" else "failed")
        assert published == []
    finally:
        monitored.close(timeout=10)


def test_notification_failure_cannot_fail_a_successful_scan(watch_environment, monkeypatch):
    svc, raw, _, days, calendar_path = watch_environment
    at_close(monkeypatch, days[349])
    replace_raw(svc, raw.filter(pl.col("date") <= days[349]))
    calls = []

    def fail_publish(events):
        calls.extend(events)
        raise RuntimeError("notification unavailable")

    monitored = HuichunModeService(
        svc.repo, calendar_path=calendar_path, publish=fail_publish,
    )
    try:
        result = scan(monitored, days[:350])
        assert len(calls) == 1 and result["candidates"]
        assert json.loads((svc.root / "snapshot.json").read_text(encoding="utf-8"))[
            "job"
        ]["status"] == "completed"
    finally:
        monitored.close(timeout=10)


def test_extension_lifespan_connects_optional_quote_service_mode_publisher(tmp_path):
    published = []
    app = FastAPI()
    app.state.repo = object()
    app.state.datastore = SimpleNamespace(data_dir=tmp_path)
    app.state.quote_service = SimpleNamespace(publish_mode_alerts=published.extend)
    app.include_router(router)
    with TestClient(app):
        service = app.state.huichun_mode_service
        assert service is not None
        assert service._publish == app.state.quote_service.publish_mode_alerts
    assert app.state.huichun_mode_service is None


def test_default_calendar_is_bundled_and_user_override_has_priority(tmp_path):
    service = HuichunModeService(object(), data_dir=tmp_path)
    try:
        assert service._calendar_path == BUNDLED_CALENDAR_PATH
        assert service._calendar_path.is_file()
    finally:
        service.close()

    override = tmp_path / "user_data" / "huichun" / "market_calendar.json"
    override.parent.mkdir(parents=True)
    override.write_bytes(BUNDLED_CALENDAR_PATH.read_bytes())
    service = HuichunModeService(object(), data_dir=tmp_path)
    try:
        assert service._calendar_path == override
    finally:
        service.close()

    explicit = tmp_path / "explicit-calendar.json"
    explicit.write_bytes(BUNDLED_CALENDAR_PATH.read_bytes())
    service = HuichunModeService(object(), data_dir=tmp_path, calendar_path=explicit)
    try:
        assert service._calendar_path == explicit
    finally:
        service.close()
