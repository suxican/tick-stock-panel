"""集合竞价指标: 猫爪取数口径、非阻塞缓存的刷新节奏、首板反馈只在注入时附带。"""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import Mock

import pytest

from app.market_time import CN_TZ
from app.plugins.meoz.provider import MeozProvider
from app.services.meoz_auction import AuctionCache

DAY = date(2026, 11, 19)


def at(hour, minute, second=0, day=19):
    return datetime(2026, 11, day, hour, minute, second, tzinfo=CN_TZ)


class FakeClient:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def query(self, api, params=None, fields=None, service="live"):
        self.calls.append((api, dict(params or {})))
        return {"stockbasic": [{"code": "000838.SZ", "symbol": "000838"}]}.get(api, self.rows)


def test_provider_converts_percent_and_suffixes(monkeypatch):
    import app.plugins.meoz.provider as mp
    monkeypatch.setattr(mp, "cn_now", lambda: at(9, 26))
    provider = MeozProvider()
    provider._client = fake = FakeClient([
        {"tradedate": "20261119", "symbol": "000838", "auc_pct_chg": 10.09, "auc_amt": 2824600,
         "auc_vol_ratio": 21.14, "ztwme": 21470011},
        {"tradedate": "20261119", "symbol": "600001", "auc_pct_chg": -1.5, "auc_amt": 100, "auc_vol_ratio": None, "ztwme": None},
        {"tradedate": "20261119", "symbol": "600002", "auc_pct_chg": None},
    ])
    out = provider.get_auction(DAY)
    assert out["000838.SZ"] == {"pct_chg": pytest.approx(0.1009), "amount": 2824600.0, "vol_ratio": 21.14,
                                "limit_bid_amount": 21470011.0}
    assert out["600001.SH"]["vol_ratio"] is None and "600002.SH" not in out
    assert ("screening", {}) in fake.calls


def test_provider_returns_empty_when_upstream_not_yet_today(monkeypatch):
    import app.plugins.meoz.provider as mp
    monkeypatch.setattr(mp, "cn_now", lambda: at(9, 16))
    provider = MeozProvider()
    provider._client = FakeClient([{"tradedate": "20261118", "symbol": "000838", "auc_pct_chg": 1.0}])
    assert provider.get_auction(DAY) == {}


def _cache(clock):
    fetch = Mock(side_effect=lambda day: {"000838.SZ": {"pct_chg": 0.1}})
    return AuctionCache(fetch=fetch, now=lambda: clock["now"], background=False), fetch


@pytest.mark.parametrize("now, calls", [(at(9, 10), 0), (at(9, 15), 1), (at(9, 40), 1)])
def test_today_starts_at_0915(now, calls):
    cache, fetch = _cache({"now": now})
    result = cache.get(DAY)
    assert fetch.call_count == calls
    assert (result is None) == (calls == 0)


def test_refreshes_every_15s_during_auction_then_finalizes_once():
    clock = {"now": at(9, 20)}
    cache, fetch = _cache(clock)
    cache.get(DAY)
    clock["now"] = at(9, 20, 10)
    cache.get(DAY)
    assert fetch.call_count == 1  # 未满 15 秒
    clock["now"] = at(9, 20, 16)
    cache.get(DAY)
    assert fetch.call_count == 2
    clock["now"] = at(9, 40)  # 上次取数早于 09:25:30 → 定版再取一次
    cache.get(DAY)
    clock["now"] = at(10, 30)
    assert cache.get(DAY)["fetched_at"].startswith("2026-11-19T09:40")
    assert fetch.call_count == 3


def test_history_day_fetched_once_future_never_and_failure_retried():
    clock = {"now": at(16, 0)}
    cache, fetch = _cache(clock)
    cache.get(date(2026, 11, 18))
    cache.get(date(2026, 11, 18))
    assert fetch.call_count == 1
    assert cache.get(date(2026, 11, 20)) is None and fetch.call_count == 1
    fetch.side_effect = RuntimeError("502")
    assert cache.get(date(2026, 11, 17)) is None
    fetch.side_effect = lambda day: {"x": {}}
    assert cache.get(date(2026, 11, 17)) is not None  # 失败后下次仍会重试


def test_background_refresh_does_not_block_reader():
    import threading
    gate = threading.Event()

    def slow(day):
        gate.wait(5)
        return {"000838.SZ": {"pct_chg": 0.1}}

    cache = AuctionCache(fetch=slow, now=lambda: at(9, 20))
    assert cache.get(DAY) is None  # 立即返回, 刷新在后台
    assert cache.get(DAY) is None  # 刷新进行中不重复起线程
    gate.set()
    for _ in range(50):
        if cache.get(DAY):
            break
        threading.Event().wait(0.05)
    assert cache.get(DAY)["rows"]["000838.SZ"]["pct_chg"] == 0.1
