"""enriched 历史缓存在盘中重写历史日之后的自愈。

复现: 盘中同步重写了 T-1 分区后, 内存历史缓存对 T-1 的代际与磁盘不一致。
- 重建要几十秒, 期间盘中行情持续写入今天的分区; 旧实现比较全局代际而整份丢弃,
  旧缓存一直留着, get_enriched_range 对 T-1 整天返回 None;
- get_enriched_history 不检查代际, 筛选/策略监控/首板悄悄读到同步前的旧数据。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.enriched_generation import bump_enriched_generation
from app.indicators import pipeline
from app.tickflow import repository as repo_module
from app.tickflow.repository import DataStore, KlineRepository

DAY = date(2026, 10, 26)
TODAY = DAY + timedelta(days=4)


def frame(day: date, close: float) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600000.SH"], "date": [day],
        "open": [close], "high": [close], "low": [close], "close": [close], "volume": [1000.0],
    })


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(repo_module, "cn_today", lambda: TODAY)
    result = KlineRepository(DataStore(tmp_path))
    result.append_enriched(pl.concat([frame(DAY + timedelta(days=i), 10.0 + i) for i in range(5)]))
    assert result.refresh_view("kline_enriched", result._enriched_glob)
    yield result
    result.db.close()


def write_during_compute(monkeypatch, write):
    """在重建计算过程中模拟并发写入。"""
    real = pipeline.compute_enriched_history_window

    def compute(*args, **kwargs):
        result = real(*args, **kwargs)
        write()
        return result

    monkeypatch.setattr(pipeline, "compute_enriched_history_window", compute)


def test_rebuild_survives_intraday_writes_to_today(repo, monkeypatch):
    write_during_compute(monkeypatch, lambda: repo.flush_live_enriched(frame(TODAY, 20.0)))
    assert repo._refresh_enriched() is False
    history = repo.get_enriched_range(DAY, TODAY - timedelta(days=1))
    assert history is not None and history["close"].to_list() == [10.0, 11.0, 12.0, 13.0]
    # 今天的行在读取后又被写入, 范围含今天时仍然 fail-closed
    assert repo.get_enriched_range(DAY, TODAY) is None
    # 写入方刚更新的最新日缓存不能被读取时的旧行覆盖
    latest, latest_day = repo.get_enriched_latest()
    assert latest_day == TODAY and latest["close"].to_list() == [20.0]


def test_rebuild_still_discards_when_an_older_day_changes(repo, monkeypatch):
    write_during_compute(monkeypatch, lambda: repo.flush_live_enriched(frame(DAY + timedelta(days=1), 99.0)))
    assert repo._refresh_enriched() is True
    assert repo._enriched_history_cache is None


def test_rebuild_discards_latest_day_drift_when_latest_is_not_today(repo, monkeypatch):
    monkeypatch.setattr(repo_module, "cn_today", lambda: TODAY + timedelta(days=3))
    write_during_compute(monkeypatch, lambda: repo.flush_live_enriched(frame(TODAY, 20.0)))
    assert repo._refresh_enriched() is True


def test_history_reader_refuses_stale_cache(repo):
    # get_enriched_history 要求缓存覆盖目标日前约 120 天的预热区间
    repo.append_enriched(pl.concat([frame(DAY - timedelta(days=i), 10.0) for i in range(1, 131)]))
    assert repo.refresh_view("kline_enriched", repo._enriched_glob)
    repo._refresh_enriched()
    assert repo.get_enriched_history(TODAY - timedelta(days=1), 2) is not None
    # 盘中同步重写了 T-1
    repo.flush_live_enriched(frame(TODAY - timedelta(days=1), 50.0))
    assert repo.get_enriched_history(TODAY - timedelta(days=1), 2) is None
    assert repo.get_enriched_range(TODAY - timedelta(days=1), TODAY - timedelta(days=1)) is None
    # 只改了今天: 更早的目标日仍可直接复用
    repo._refresh_enriched()
    repo.flush_live_enriched(frame(TODAY, 21.0))
    assert repo.get_enriched_history(TODAY - timedelta(days=1), 2) is not None


def test_stale_cache_schedules_one_background_rebuild_when_enabled(repo, monkeypatch):
    starts = []
    monkeypatch.setattr(repo, "_start_enriched_warmup", lambda: starts.append(1))
    repo._refresh_enriched()
    bump_enriched_generation(repo.store.data_dir)
    # 未启用(测试、离线脚本)时只降级, 不起后台线程
    assert repo.get_enriched_range(DAY, DAY) is None and starts == []
    repo.enable_stale_history_rebuild()
    assert repo.get_enriched_range(DAY, DAY) is None
    assert repo.get_enriched_history(DAY, 1) is None
    assert starts == [1], "限流: 一段时间内只触发一次"
    monkeypatch.setattr(repo_module, "_STALE_REBUILD_INTERVAL_SECONDS", 0.0)
    repo.get_enriched_range(DAY, DAY)
    assert starts == [1, 1]


def test_intraday_writes_to_today_never_trigger_rebuild_or_starve_monitors(repo, monkeypatch):
    """盘中今天的分区每隔几秒就变: 不能因此反复重建, 也不能让盘中策略监控
    (以今天为目标日读历史, 再用实时行情替换今天的行)退到慢路径。"""
    repo.append_enriched(pl.concat([frame(DAY - timedelta(days=i), 10.0) for i in range(1, 131)]))
    assert repo.refresh_view("kline_enriched", repo._enriched_glob)
    starts = []
    monkeypatch.setattr(repo, "_start_enriched_warmup", lambda: starts.append(1))
    repo.enable_stale_history_rebuild()
    repo._refresh_enriched()
    repo.flush_live_enriched(frame(TODAY, 30.0))
    history = repo.get_enriched_history(TODAY, 2)
    assert history is not None and TODAY - timedelta(days=1) in history["date"].to_list()
    assert repo.get_enriched_range(DAY, TODAY) is None   # 含今天的范围仍 fail-closed
    assert starts == []


def test_rebuild_after_heal_serves_rewritten_reference_day(repo):
    """端到端: 重写 T-1 → 过期 → 重建 → 读到新值。"""
    repo._refresh_enriched()
    repo.flush_live_enriched(frame(TODAY - timedelta(days=1), 50.0))
    assert repo.get_enriched_range(TODAY - timedelta(days=1), TODAY - timedelta(days=1)) is None
    repo._refresh_enriched()
    healed = repo.get_enriched_range(TODAY - timedelta(days=1), TODAY - timedelta(days=1))
    assert healed is not None and healed["close"].to_list() == [50.0]
