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


@pytest.mark.parametrize("cutoff_index,event_type,pool", [
    (348, "pending_cross", "observations"),
    (349, "a0_confirmed", "candidates"),
])
def test_latest_scan_publishes_dated_raw_price_events_after_persistence(
    watch_environment, cutoff_index, event_type, pool,
):
    svc, raw, factors, days, calendar_path = watch_environment
    cutoff = days[cutoff_index]
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


def test_notification_failure_cannot_fail_a_successful_scan(watch_environment):
    svc, raw, _, days, calendar_path = watch_environment
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
