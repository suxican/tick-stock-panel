"""ETF/指数日K: 盘中拉到的当日 bar 不能被当成定版历史。

2026-11-13 盘中 10:04 手动跑管道, ETF 当日未收盘 bar 无 quote_ts 落盘, 完整性
检查视为权威历史; 当天 15:35 后端停机, 之后的管道从最新日重拉, 但 naive 起点被
_datetime_to_ms 按宿主机时区换算 (UTC-8 主机晚 16 小时), 11-13 落在窗口外,
残缺量 (510300 约 131 万手, 实际 851 万手) 永久留存。
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from app.market_time import CN_TZ
from app.services import data_integrity, index_sync, kline_sync, preferences
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository

SYMBOL = {"etf": "510300.SH", "index": "000300.SH"}


class _FakeKlines:
    """按请求窗口 (两端含) 返回当时可得的日K, 时间戳与 TickFlow 一致为北京 00:00。"""

    def __init__(self) -> None:
        self.bars: dict[date, float] = {}

    def batch(self, symbols, *, start_time, end_time, **kwargs):
        days = [d for d in sorted(self.bars)
                if start_time <= int(datetime.combine(d, datetime.min.time(), CN_TZ).timestamp() * 1000) <= end_time]
        ms = [int(datetime.combine(d, datetime.min.time(), CN_TZ).timestamp() * 1000) for d in days]
        return {
            symbol: {
                "timestamp": ms, "open": [4.1] * len(days), "high": [4.2] * len(days),
                "low": [4.0] * len(days), "close": [4.12] * len(days),
                "volume": [self.bars[d] for d in days], "amount": [self.bars[d] * 412 for d in days],
            }
            for symbol in symbols
        }


@pytest.fixture
def source(monkeypatch):
    klines = _FakeKlines()
    monkeypatch.setattr(kline_sync, "get_client", lambda: type("Client", (), {"klines": klines})())
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tickflow")
    monkeypatch.setattr(preferences, "get_index_daily_batch_size", lambda: 100)
    return klines


def _sync(asset, repo, monkeypatch, now, start, end):
    monkeypatch.setattr(index_sync, "cn_now", lambda: now)
    sync = index_sync.sync_and_persist_etf_daily if asset == "etf" else index_sync.sync_and_persist_index_daily
    caps = CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits()})
    return sync(repo, caps, start_date=start, end_date=end, symbols_override=[SYMBOL[asset]])


def _volume(tmp_path, table, day):
    import polars as pl

    return pl.read_parquet(tmp_path / table / f"date={day}" / "part.parquet")["volume"].to_list()


@pytest.mark.parametrize("asset", ["etf", "index"])
def test_intraday_bar_is_a_snapshot_until_a_later_run_refetches_it(asset, source, monkeypatch, tmp_path):
    table = f"kline_{asset}_daily"
    repo = KlineRepository(DataStore(tmp_path))
    friday, monday = date(2026, 11, 13), date(2026, 11, 16)

    # 盘中手动管道: 当日 bar 只有上午的量
    source.bars = {date(2026, 11, 12): 11_631_446, friday: 1_316_688}
    _sync(asset, repo, monkeypatch, datetime(2026, 11, 13, 10, 4, tzinfo=CN_TZ),
          datetime(2026, 11, 12), datetime(2026, 11, 13))
    issue = data_integrity.inspect_daily_partition(tmp_path, table, friday)
    assert issue is not None and issue.kind == "snapshot" and issue.symbols == (SYMBOL[asset],)
    assert data_integrity.inspect_daily_partition(tmp_path, table, date(2026, 11, 12)) is None

    # 停机错过当日盘后; 下个交易日盘后从最新日 (北京墙钟 naive) 重拉
    source.bars.update({friday: 8_510_502, monday: 19_985_629})
    _sync(asset, repo, monkeypatch, datetime(2026, 11, 16, 15, 47, tzinfo=CN_TZ),
          datetime(2026, 11, 13), datetime(2026, 11, 16))
    assert _volume(tmp_path, table, friday) == [8_510_502]
    assert data_integrity.inspect_daily_partition(tmp_path, table, friday) is None
    assert data_integrity.inspect_daily_partition(tmp_path, table, monday) is None


def test_daily_batch_reads_naive_window_as_beijing_wall_clock(source):
    source.bars = {date(2026, 11, 13): 8_510_502, date(2026, 11, 16): 19_985_629}
    frame = kline_sync.sync_daily_batch(
        ["510300.SH"], start_time=datetime(2026, 11, 13), end_time=datetime(2026, 11, 16),
    )
    assert sorted(frame["date"].to_list()) == [date(2026, 11, 13), date(2026, 11, 16)]
