"""集合竞价指标 (猫爪 screening) 的非阻塞缓存, 供首板「昨日首板反馈」展示与排序。

- 读取方 (首板后台 worker, 每次行情回调) 只读内存, 从不在调用线程里请求网络;
  缓存过期时起一个后台线程刷新, 同一时刻最多一个刷新。
- 当日: 09:15 前不取; 09:15–09:31 每 15 秒刷新 (竞价期间撮合价持续变化); 09:31 后若最近一次
  取数早于 09:25:30 再取一次定版, 此后当日不再请求。历史日: 取一次后不再请求。
- 只用于展示与排序, 不进入优先分、买入提示或任何规则。猫爪不可用时返回空, 页面不显示竞价列。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import date, datetime, time

from app.data_providers import custom as custom_sources
from app.market_time import cn_now

logger = logging.getLogger(__name__)

START, FINAL, LIVE_UNTIL = time(9, 15), time(9, 25, 30), time(9, 31)
REFRESH_S = 15.0


def _default_fetch(day: date) -> dict[str, dict]:
    provider = custom_sources.get_provider("meoz")
    fetch = getattr(provider, "get_auction", None)
    if not callable(fetch):
        raise ValueError("当前猫爪插件版本不支持集合竞价指标")
    return fetch(day)


class AuctionCache:
    def __init__(self, fetch: Callable[[date], dict[str, dict]] = _default_fetch,
                 now: Callable[[], datetime] = cn_now, background: bool = True) -> None:
        self._fetch, self._now, self._background = fetch, now, background
        self._lock = threading.Lock()
        self._data: dict[date, tuple[datetime, dict[str, dict]]] = {}
        self._refreshing: set[date] = set()

    def _due(self, day: date, now: datetime) -> bool:
        hit = self._data.get(day)
        if day > now.date():
            return False
        if day < now.date():
            return hit is None
        clock = now.time().replace(tzinfo=None)
        if clock < START:
            return False
        if hit is None:
            return True
        fetched = hit[0].time().replace(tzinfo=None)
        if clock < LIVE_UNTIL:
            return (now - hit[0]).total_seconds() >= REFRESH_S
        return fetched < FINAL

    def get(self, day: date) -> dict | None:
        """{fetched_at, rows: {symbol: {...}}} 或 None (尚无数据)。必要时触发后台刷新。"""
        now = self._now()
        with self._lock:
            hit = self._data.get(day)
            due = self._due(day, now) and day not in self._refreshing
            if due:
                self._refreshing.add(day)
        if due:
            if self._background:
                threading.Thread(target=self._refresh, args=(day,), name="meoz-auction", daemon=True).start()
            else:
                self._refresh(day)
                with self._lock:
                    hit = self._data.get(day)
        return {"fetched_at": hit[0].isoformat(timespec="seconds"), "rows": hit[1]} if hit else None

    def _refresh(self, day: date) -> None:
        try:
            rows = self._fetch(day)
            if rows:
                with self._lock:
                    self._data = {day: (self._now(), rows)}  # 只保留当前查询日
        except Exception as exc:  # noqa: BLE001 -- 可选展示数据, 失败只记日志并等待下次
            logger.info("集合竞价指标获取失败: %s", type(exc).__name__)
        finally:
            with self._lock:
                self._refreshing.discard(day)


_cache: AuctionCache | None = None


def get_cache() -> AuctionCache:
    global _cache
    if _cache is None:
        _cache = AuctionCache()
    return _cache


def lookup():
    """首板反馈注入用的读取函数 (进程级单例缓存)。"""
    return get_cache().get
