"""盘中剧本核验: 开盘分钟数据不全时补拉 09:25–10:00 后重新判定。"""
import threading
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl

from app.services import market_recap_intraday as mri
from app.services import minute_shared_pull

DAY = date(2026, 11, 17)


def _repo(tmp_path, symbols):
    return SimpleNamespace(
        get_instruments=lambda: pl.DataFrame({"symbol": symbols}),
        store=SimpleNamespace(data_dir=tmp_path),
    )


def _write(tmp_path, bars: dict[str, int]):
    """bars: symbol → 从 09:31 起连续的分钟根数。"""
    base = datetime.combine(DAY, datetime.min.time()).replace(hour=9, minute=31)
    rows = [{"symbol": s, "datetime": base + timedelta(minutes=m), "amount": 1.0}
            for s, n in bars.items() for m in range(n)]
    p = tmp_path / "kline_minute" / f"date={DAY.isoformat()}" / "part.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(p)


def test_gap_symbols_include_partial_and_absent(tmp_path):
    _write(tmp_path, {"000001.SZ": 30, "000002.SZ": 12, "000003.SZ": 45})
    repo = _repo(tmp_path, ["000001.SZ", "000002.SZ", "000003.SZ", "600000.SH"])
    assert mri.opening_gap_symbols(repo, DAY) == ["000002.SZ", "600000.SH"]


def test_gap_symbols_without_partition_is_whole_universe(tmp_path):
    repo = _repo(tmp_path, ["600000.SH", "000001.SZ"])
    assert mri.opening_gap_symbols(repo, DAY) == ["000001.SZ", "600000.SH"]


def test_backfill_pulls_only_gaps_over_opening_window(tmp_path, monkeypatch):
    _write(tmp_path, {"000001.SZ": 30, "000002.SZ": 3})
    repo = _repo(tmp_path, ["000001.SZ", "000002.SZ"])
    calls = []

    def fake_pull(repo_, symbols, **kw):
        calls.append((symbols, kw))
        return pl.DataFrame({"symbol": ["000002.SZ"] * 2})

    monkeypatch.setattr(mri, "_minute_source_allowed", lambda capset: True)
    monkeypatch.setattr(minute_shared_pull, "pull", fake_pull)
    out = mri.backfill_opening_minutes(repo, DAY, None)
    assert out == {"status": "ok", "symbols": 1, "rows": 2}
    symbols, kw = calls[0]
    assert symbols == ["000002.SZ"]
    assert kw["asset_type"] == "stock"
    assert kw["start"] == datetime(2026, 11, 17, 9, 25)
    assert kw["end"] == datetime(2026, 11, 17, 10, 0)


def test_backfill_skips_when_complete_or_source_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(minute_shared_pull, "pull", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    _write(tmp_path, {"000001.SZ": 30})
    repo = _repo(tmp_path, ["000001.SZ"])
    assert mri.backfill_opening_minutes(repo, DAY, None)["status"] == "complete"
    repo = _repo(tmp_path, ["000001.SZ", "000002.SZ"])
    monkeypatch.setattr(mri, "_minute_source_allowed", lambda capset: False)
    assert mri.backfill_opening_minutes(repo, DAY, None) == {"status": "unavailable", "symbols": 1, "rows": 0}


def test_backfill_failure_is_reported_not_raised(tmp_path, monkeypatch):
    repo = _repo(tmp_path, ["000001.SZ"])
    monkeypatch.setattr(mri, "_minute_source_allowed", lambda capset: True)

    def boom(*a, **k):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(minute_shared_pull, "pull", boom)
    assert mri.backfill_opening_minutes(repo, DAY, None)["status"] == "failed"


class _Store:
    def __init__(self):
        self.saved = []

    def save_report(self, r):
        self.saved.append(r)
        return r

    def list_reports(self):
        return list(reversed(self.saved))


def _checks(monkeypatch, statuses):
    seq = iter(statuses)
    monkeypatch.setattr(mri, "run_intraday_check", lambda repo, now=None: {
        "date": DAY.isoformat(), "status": next(seq), "message": "开盘分钟数据不完整"})


def test_run_and_record_rechecks_after_backfill(monkeypatch):
    store = _Store()
    monkeypatch.setattr(mri, "_store", store)
    _checks(monkeypatch, ["data_incomplete", "ok"])
    monkeypatch.setattr(mri, "backfill_opening_minutes",
                        lambda repo, day, capset, on_updated=None: {"status": "ok", "symbols": 9, "rows": 90})
    result = mri.run_and_record(None, push=False, backfill=True)
    assert result["status"] == "ok"
    assert result["backfill"]["symbols"] == 9
    assert [r["status"] for r in store.saved] == ["ok"]


def test_run_and_record_explains_unavailable_source(monkeypatch):
    monkeypatch.setattr(mri, "_store", _Store())
    _checks(monkeypatch, ["data_incomplete"])
    monkeypatch.setattr(mri, "backfill_opening_minutes",
                        lambda repo, day, capset, on_updated=None: {"status": "unavailable", "symbols": 9, "rows": 0})
    result = mri.run_and_record(None, push=False, backfill=True)
    assert result["status"] == "data_incomplete"
    assert "无法补拉" in result["message"]


def test_run_and_record_without_backfill_keeps_old_behavior(monkeypatch):
    monkeypatch.setattr(mri, "_store", _Store())
    _checks(monkeypatch, ["data_incomplete"])
    monkeypatch.setattr(mri, "backfill_opening_minutes",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no backfill")))
    assert "backfill" not in mri.run_and_record(None, push=False)


def test_run_manual_backfills_in_background_once(monkeypatch):
    store = _Store()
    monkeypatch.setattr(mri, "_store", store)
    monkeypatch.setattr(mri, "run_intraday_check", lambda repo, now=None: {
        "date": DAY.isoformat(), "status": "data_incomplete" if not store.saved else "ok", "message": "x"})
    release, started = threading.Event(), threading.Event()
    pulls = []

    def slow_backfill(repo, day, capset, on_updated=None):
        pulls.append(day)
        started.set()
        release.wait(5)
        store.saved.append({"date": DAY.isoformat(), "status": "placeholder"})  # 补完后重判转为 ok
        return {"status": "ok", "symbols": 1, "rows": 1}

    monkeypatch.setattr(mri, "backfill_opening_minutes", slow_backfill)
    now = datetime(2026, 11, 17, 11, 50)
    first = mri.run_manual(None, None, now=now)
    assert first["backfilling"] is True
    assert started.wait(5)
    assert mri.is_backfilling(DAY.isoformat())
    second = mri.run_manual(None, None, now=now)
    assert second["backfilling"] is True
    release.set()
    for _ in range(100):
        if not mri.is_backfilling(DAY.isoformat()):
            break
        threading.Event().wait(0.02)
    assert not mri.is_backfilling(DAY.isoformat())
    assert len(pulls) == 1
    assert store.saved[-1]["status"] == "ok"


def test_run_manual_complete_data_records_directly(monkeypatch):
    store = _Store()
    monkeypatch.setattr(mri, "_store", store)
    monkeypatch.setattr(mri, "run_intraday_check", lambda repo, now=None: {
        "date": DAY.isoformat(), "status": "ok"})
    out = mri.run_manual(None, None, now=datetime(2026, 11, 17, 10, 30))
    assert out == {"result": {"date": DAY.isoformat(), "status": "ok"}, "backfilling": False}
    assert len(store.saved) == 1
