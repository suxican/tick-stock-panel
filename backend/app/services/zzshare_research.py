"""题材雷达只读查询：多个页面/客户端共用采集，不跨日期补数据。"""

from collections import OrderedDict
from concurrent.futures import Future
from datetime import date, time, timedelta
from enum import IntEnum
from threading import Lock
from time import monotonic
from typing import Literal

from app.data_providers import custom as custom_sources
from app.market_time import cn_now

ResearchKind = Literal[
    "plates_list", "plates_rank", "market_sentiment", "sentiment_trend",
    "ths_hot_top", "lhb_list", "lhb_detail", "lhb_stock_history",
]


class PlateType(IntEnum):
    INDUSTRY = 14
    CONCEPT = 15
    THEME = 17


class ResearchUnavailableError(RuntimeError):
    pass


class ResearchError(RuntimeError):
    pass


class ResearchRateLimitError(ResearchError):
    pass


RangeKind = Literal["sentiment_daily", "effect_daily", "market_sentiment"]


def _fetch(kind: str, day: date, code: str | None, plate_type: PlateType | None,
           start: date | None = None) -> list:
    try:
        provider = custom_sources.get_provider("zzshare")
    except ValueError as exc:
        raise ResearchUnavailableError("ZZShare 数据源未就绪") from exc
    fetch = getattr(provider, "research", None)
    if not callable(fetch):
        raise ResearchUnavailableError("ZZShare 插件缺少研究接口")
    opts = {"code": code} if code else {}
    if plate_type is not None:
        opts["plate_type"] = int(plate_type)
    if start is not None:
        opts["start"] = start.isoformat()
    elif kind == "market_sentiment":
        opts["start"] = (day - timedelta(days=29)).isoformat()
    if kind in ("plates_rank", "ths_hot_top", "lhb_stock_history"):
        opts["limit"] = 100
    try:
        rows = fetch(kind, day.isoformat(), **opts)
    except RuntimeError as exc:
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            raise ResearchUnavailableError("ZZShare 未授权访问该数据，请检查数据源凭据") from exc
        if status == 429:
            raise ResearchRateLimitError("ZZShare 达到请求频率限制，请稍后再试") from exc
        raise ResearchError("ZZShare 请求失败，请稍后重试") from exc
    if not isinstance(rows, list):
        raise ResearchError("ZZShare 返回的数据格式异常")
    return rows


def fetch_range(kind: RangeKind, start: date, end: date) -> list:
    """Uncached daily ranges for callers that keep their own archive.

    These kinds are not part of ``ResearchKind``, so the public research route
    and the radar page cache never expose or mix them.
    """
    if kind not in ("sentiment_daily", "effect_daily", "market_sentiment"):
        raise ValueError("未知区间数据集")
    if start > end:
        raise ValueError("起始日期不得晚于结束日期")
    return _fetch(kind, end, None, None, start=start)


class ResearchReader:
    """小容量进程缓存；同一请求合并，网络等待不持锁。"""

    def __init__(self):
        self._lock = Lock()
        self._cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
        self._pending: dict[tuple, Future] = {}

    def read(self, kind: ResearchKind, day: date, code: str | None = None, plate_type: PlateType | None = None) -> dict:
        now = cn_now()
        if day > now.date():
            raise ValueError("不能查询未来日期")
        needs_code = kind in ("lhb_detail", "lhb_stock_history")
        if needs_code != bool(code):
            raise ValueError("席位明细和个股历史需要股票代码，其他查询不接受股票代码")
        if kind in ("plates_list", "plates_rank"):
            plate_type = plate_type or PlateType.THEME
        elif plate_type is not None:
            raise ValueError("仅板块榜和目录支持板块类别")
        iso = day.isoformat()
        base = {"kind": kind, "date": iso, "source": "zzshare", "stale": False}
        if kind in ("lhb_list", "lhb_detail") and day == now.date() and now.time() < time(15):
            return {**base, "rows": [], "fetched_at": None, "warning": "当日龙虎榜盘后披露。"}
        key = (kind, day, code, plate_type)
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > monotonic():
                self._cache.move_to_end(key)
                return cached[1]
            wait = self._pending.get(key)
            if wait is None:
                self._pending[key] = task = Future()
        if wait is not None:
            return wait.result()
        try:
            try:
                rows = _fetch(kind, day, code, plate_type)
                warning = "板块目录为当前资料，不代表历史成分。" if kind == "plates_list" else None
                result = {**base, "rows": rows, "fetched_at": cn_now().isoformat(timespec="seconds"),
                          "warning": warning}
                ttl = 60 if kind == "sentiment_trend" else 300
                if day < now.date() or kind == "plates_list":
                    ttl = 3600
                if not rows:
                    ttl = 60
            except (ResearchError, ResearchUnavailableError) as exc:
                if not cached or not cached[1]["rows"]:
                    raise
                result = {**cached[1], "stale": True, "warning": f"{exc}；显示上次成功读取的数据。"}
                ttl = 30
            with self._lock:
                self._cache[key] = monotonic() + ttl, result
                self._cache.move_to_end(key)
                if len(self._cache) > 128:
                    self._cache.popitem(last=False)
            task.set_result(result)
            return result
        except Exception as exc:
            task.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._pending.pop(key, None)


# The radar page and optional first-board enrichment share provider quota and cache.
shared_reader = ResearchReader()
