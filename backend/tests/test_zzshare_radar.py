import time
from datetime import date, datetime
from threading import Event
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.plugins.zzshare import ZzshareError
from app.services import zzshare_radar as radar

DAY = date(2026, 10, 21)
DS = DAY.isoformat()


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    stamp = datetime(2026, 11, 4, 18, tzinfo=CN_TZ)
    monkeypatch.setattr(radar, "cn_now", lambda: stamp)
    monkeypatch.setattr(radar, "cn_today", stamp.date)


def quotes(day=DAY):
    return pl.DataFrame([
        {"symbol": f"60000{i}.SH", "date": day, "change_pct": change, "raw_close": close,
         "consecutive_limit_ups": int(close == 11), "consecutive_limit_downs": int(close == 9),
         "amount": 5000.0 if i == 2 else 1000.0, "volume": 100}
        for i, (change, close) in enumerate([(0.1, 11), (.05, 10.5), (.06, 10.6), (0, 10), (-.1, 9)])
    ])


def members(code, count=5):
    return [{"trade_date": DS, "plate_code": code, "plate_type": 17,
             "stock_code": f"60000{i}", "stock_name": f"股{i}"}
            for i in range(count)]


class Provider:
    def __init__(self):
        self.requests = []
        self.wait = None
        self.fail = False

    def research(self, kind, day, *, code=None, plate_type=17, limit=20):
        assert day == DS and plate_type == 17
        if kind == "plates_rank":
            assert limit == 1000
            return [{"trade_date": DS, "plate_code": theme, "plate_name": theme, "plate_type": 17}
                    for theme in ["A", "B"]]
        assert kind == "plates_stocks"
        self.requests.append(code)
        if code == "B" and self.wait:
            self.wait.wait(timeout=5)
        if code == "B" and self.fail:
            raise RuntimeError("private upstream failure")
        return members(code, count=6 if code == "A" else 7)


@pytest.fixture
def env(tmp_path, monkeypatch):
    provider = Provider()
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(radar.custom_sources, "get_provider", lambda _: provider)
    monkeypatch.setattr(radar, "_quote_frame", lambda _self, _day: quotes())
    return repo, provider


def finish(repo):
    for _ in range(200):
        report = radar.get_themes(repo, DAY)
        if report["state"] != "running":
            return report
        time.sleep(.02)
    pytest.fail("background sync did not finish")


def test_read_only_start_progress_and_same_date_membership_cache(env):
    repo, provider = env
    assert radar.get_themes(repo, DAY)["state"] == "idle"
    assert not list(repo.store.data_dir.iterdir())
    provider.wait = Event()
    try:
        assert radar.start_themes_sync(repo, DAY)["state"] == "running"
        busy = radar.start_themes_sync(repo, date(2026, 10, 20))
        assert busy["state"] == "idle" and DS in busy["message"]
        for _ in range(100):
            if "B" in provider.requests:
                break
            time.sleep(.02)
        partial = radar.get_themes(repo, DAY)
        assert partial["progress"] == {"done": 1, "total": 2}
        assert partial["state"] == "running"
        assert radar.start_themes_sync(repo, DAY)["state"] == "running"
        assert provider.requests == ["A", "B"]
    finally:
        provider.wait.set()
    result = finish(repo)
    assert result["state"] == "ready"
    assert result["coverage"] == {"candidates": 2, "completed": 2, "eligible": 1}
    row = result["rows"][0]
    assert row["member_count"] == 6 and row["valid_count"] == 5
    assert row["coverage_pct"] == pytest.approx(83.33, abs=.01)
    assert row["rising_count"] == 3 and row["strong_count"] == 2
    assert row["avg_change_pct"] == pytest.approx(2.2)
    assert row["score"] == pytest.approx(69.6)
    assert row["limit_up_count"] == row["limit_down_count"] == 1
    assert row["leader"]["symbol"] == "600000.SH"
    assert row["anchor"]["symbol"] == "600002.SH"
    assert result["quote_asof"] is None
    radar.start_themes_sync(repo, DAY)
    assert finish(repo)["rows"] == result["rows"]
    assert provider.requests == ["A", "B"]
    assert radar.get_themes(repo, date(2026, 10, 20))["rows"] == []


def test_incomplete_not_reported_as_complete_resume_only_missing(env):
    repo, provider = env
    provider.fail = True
    radar.start_themes_sync(repo, DAY)
    partial = finish(repo)
    assert partial["state"] == "error" and partial["coverage"]["completed"] == 1
    assert "private" not in partial["message"]
    provider.fail = False
    radar.start_themes_sync(repo, DAY)
    assert finish(repo)["state"] == "ready"
    assert provider.requests == ["A", "B", "B"]


def test_no_other_day_or_guessed_price_limits(env, monkeypatch):
    repo, provider = env
    for frame in [quotes(date(2026, 10, 20)), quotes().drop("consecutive_limit_ups")]:
        monkeypatch.setattr(radar, "_quote_frame", lambda _self, _day, frame=frame: frame)
        radar.start_themes_sync(repo, DAY)
        assert finish(repo)["state"] == "no_data"
        assert not provider.requests


def test_disk_restore_and_quote_refresh_without_refetching_members(env, monkeypatch):
    repo, provider = env
    radar.start_themes_sync(repo, DAY)
    first = finish(repo)
    monkeypatch.setattr(radar, "_task", None)
    assert radar.get_themes(repo, DAY) == first
    monkeypatch.setattr(radar, "_quote_frame",
                        lambda *_: quotes().with_columns(pl.lit(-.01).alias("change_pct")))
    radar.start_themes_sync(repo, DAY)
    changed = finish(repo)
    assert changed["state"] == "ready" and not changed["rows"]
    assert provider.requests == ["A", "B"]


def test_failed_refresh_preserves_prior_complete_snapshot_and_reports_failure(env, monkeypatch):
    repo, provider = env
    radar.start_themes_sync(repo, DAY)
    ready = finish(repo)
    def broken(*args, **kwargs):
        raise RuntimeError("secret")
    monkeypatch.setattr(provider, "research", broken)
    radar.start_themes_sync(repo, DAY)
    failed = finish(repo)
    assert failed["state"] == "error" and failed["rows"] == ready["rows"]
    assert failed["computed_at"] == ready["computed_at"] and "secret" not in failed["message"]
    monkeypatch.setattr(radar, "_task", None)
    assert radar.get_themes(repo, DAY) == ready


def test_no_duplicate_instruments_and_wrong_day_quote_stamps(env, monkeypatch):
    repo, _ = env
    base = quotes().with_columns(pl.lit(None, dtype=pl.Int64).alias("quote_ts"))
    base = base.with_columns(pl.when(pl.col("symbol") == "600002.SH").then(1500)
                            .otherwise(pl.col("quote_ts")).alias("quote_ts"))
    base = pl.concat([base, base.head(1)])
    monkeypatch.setattr(radar, "_quote_frame", lambda *_: base)
    actual, stamp = radar._quotes(repo, DAY)
    assert set(actual) == {"600001", "600003", "600004"} and stamp is None


def test_thresholds_include_only_qualified_boards_and_all_sorted_candidates(env, monkeypatch):
    repo, provider = env
    base = quotes().with_columns(pl.when(pl.col("symbol") == "600003.SH").then(-.005)
                                 .otherwise(pl.col("change_pct")).alias("change_pct"))
    monkeypatch.setattr(radar, "_quote_frame", lambda *_: base)
    def many(kind, day, *, code=None, **kwargs):
        if kind == "plates_rank":
            return [{"trade_date": DS, "plate_type": 17, "plate_code": code, "plate_name": code}
                    for code in ["D", "B", "C", "A", "insufficient"]]
        return members(code, count=4 if code == "insufficient" else 5)
    monkeypatch.setattr(provider, "research", many)
    radar.start_themes_sync(repo, DAY)
    assert [r["plate_code"] for r in finish(repo)["rows"]] == list("ABCD")


def test_corrupt_report_cannot_be_served_or_break_resync(env):
    repo, _ = env
    directory = radar._directory(repo, DAY)
    directory.mkdir(parents=True)
    (directory / "report.json").write_text('{"date":"2026-10-21","state":"ready","rows":[]}', encoding="utf-8")
    assert radar.get_themes(repo, DAY)["state"] == "error"
    radar.start_themes_sync(repo, DAY)
    assert finish(repo)["state"] == "ready"


def test_refresh_keeps_prior_ranking_and_time_while_new_candidates_load(env, monkeypatch):
    repo, provider = env
    radar.start_themes_sync(repo, DAY)
    old = finish(repo)
    original = provider.research
    entered, release = Event(), Event()
    def wait_for_new(kind, day, *, code=None, **kwargs):
        if kind == "plates_rank":
            return [*original(kind, day, **kwargs), {"trade_date": DS, "plate_type": 17, "plate_code": "new", "plate_name": "新增"}]
        entered.set()
        release.wait(timeout=5)
        return members(code)
    monkeypatch.setattr(provider, "research", wait_for_new)
    try:
        assert radar.start_themes_sync(repo, DAY)["rows"] == old["rows"]
        assert entered.wait(timeout=5)
        refreshing = radar.get_themes(repo, DAY)
        assert refreshing["state"] == "running" and refreshing["computed_at"] == old["computed_at"]
        assert refreshing["rows"] == old["rows"] and refreshing["progress"] == {"done": 2, "total": 3}
    finally:
        release.set()
    assert finish(repo)["coverage"]["completed"] == 3


def test_anchor_must_be_rising_and_have_positive_amount(env, monkeypatch):
    repo, _ = env
    base = quotes().with_columns(pl.when(pl.col("change_pct") <= 0).then(9000.).otherwise(None).alias("amount"))
    monkeypatch.setattr(radar, "_quote_frame", lambda *_: base)
    radar.start_themes_sync(repo, DAY)
    assert finish(repo)["rows"][0]["anchor"] is None
    base = base.with_columns(pl.when(pl.col("symbol") == "600000.SH").then(100.)
                             .otherwise(pl.col("amount")).alias("amount"))
    radar.start_themes_sync(repo, DAY)
    row = finish(repo)["rows"][0]
    assert row["anchor"] == row["leader"]


@pytest.mark.parametrize("status", [429, 401, 403])
def test_rate_and_permission_failures_stop_immediately(env, monkeypatch, status):
    repo, provider = env
    original = provider.research
    attempted = []
    def limited(kind, day, *, code=None, **kwargs):
        if kind == "plates_rank":
            return original(kind, day, **kwargs)
        attempted.append(code)
        raise ZzshareError("private", status_code=status)
    monkeypatch.setattr(provider, "research", limited)
    radar.start_themes_sync(repo, DAY)
    failed = finish(repo)
    assert failed["state"] == "error" and str(status) in failed["message"]
    assert attempted == ["A"] and failed["coverage"]["completed"] == 0


def test_empty_members_are_not_cached_and_do_not_enter_denominator(env, monkeypatch):
    repo, provider = env
    original = provider.research
    def empty(kind, day, *, code=None, **kwargs):
        if kind == "plates_stocks" and code == "A":
            return []
        return original(kind, day, code=code, **kwargs)
    monkeypatch.setattr(provider, "research", empty)
    radar.start_themes_sync(repo, DAY)
    assert finish(repo)["state"] == "error"
    monkeypatch.setattr(provider, "research", original)
    radar.start_themes_sync(repo, DAY)
    assert finish(repo)["coverage"]["completed"] == 2
    assert provider.requests == ["B", "A"]


def test_real_storage_schema_uses_adjacent_sessions_and_engine_board_counts(tmp_path):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    def write(day, close, raw, ups):
        target = tmp_path / "kline_daily_enriched" / f"date={day}" / "part.parquet"
        target.parent.mkdir(parents=True)
        pl.DataFrame({
            "symbol": ["600001.SH"], "date": [day], "close": [close], "raw_close": [raw],
            "volume": [100.0], "amount": [2000.0],
            "consecutive_limit_ups": pl.Series([ups], dtype=pl.UInt32),
            "consecutive_limit_downs": pl.Series([0], dtype=pl.UInt32),
            "quote_ts": pl.Series([None], dtype=pl.Int64),
        }).write_parquet(target)
    write(date(2026, 10, 19), 8.0, 16., 0)
    write(DAY, 11., 11., 1)
    assert radar._quotes(repo, DAY) == ({}, None)  # 不把隔了一个交易日的价格当昨收。
    write(date(2026, 10, 20), 10., 20., 0)
    entries, stamp = radar._quotes(repo, DAY)
    assert entries["600001"]["change"] == pytest.approx(.1) and entries["600001"]["limit_up"]
    assert entries["600001"]["limit_down"] is False and stamp is None


def test_bad_memberships_do_not_skip_later_valid_boards(env, monkeypatch):
    repo, provider = env
    def some_empty(kind, day, *, code=None, **kwargs):
        if kind == "plates_rank":
            return [{"trade_date": day, "plate_name": code, "plate_type": 17, "plate_code": code}
                    for code in "ABCD"]
        return members(code) if code == "D" else []
    monkeypatch.setattr(provider, "research", some_empty)
    radar.start_themes_sync(repo, DAY)
    partial = finish(repo)
    assert partial["state"] == "error" and partial["progress"] == {"done": 4, "total": 4}
    assert partial["coverage"]["completed"] == 1 and partial["rows"][0]["plate_code"] == "D"
