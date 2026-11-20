"""猫爪最强风口 (fengk_kp) 读取: 实时缓存 60 秒, 历史日按 tradedate 查询并进程内缓存, 不落盘。

只作展示证据, 不参与首板优先分、候选判定或主线评分。
"""
from __future__ import annotations

import threading
import time as time_mod
from collections import OrderedDict
from datetime import date

from app.data_providers import custom as custom_sources
from app.market_time import cn_now

LIVE_TTL_S = 60.0
HISTORY_MAX = 16

_lock = threading.Lock()
_live: tuple[float, dict] | None = None
_history: OrderedDict[date, dict] = OrderedDict()


class StrongestVentUnavailableError(RuntimeError):
    """猫爪插件未加载或不支持最强风口。"""


def _provider():
    try:
        provider = custom_sources.get_provider("meoz")
    except ValueError as exc:
        raise StrongestVentUnavailableError("猫爪插件未就绪") from exc
    if not callable(getattr(provider, "get_strongest_vent", None)):
        raise StrongestVentUnavailableError("当前猫爪插件版本不支持最强风口")
    return provider


def read(day: date | None) -> dict:
    """{fetched_at, trade_date, rows}; day=None 为实时。成功但为空是真实的空榜。"""
    global _live
    if day is not None and day > cn_now().date():
        raise ValueError("不能查询未来日期")
    if day is None:
        with _lock:
            hit = _live
        if hit and time_mod.monotonic() - hit[0] < LIVE_TTL_S:
            return hit[1]
    else:
        with _lock:
            cached = _history.get(day)
            if cached is not None:
                _history.move_to_end(day)
                return cached
    rows = _provider().get_strongest_vent(day)
    packet = {"fetched_at": cn_now().isoformat(timespec="seconds"),
              "trade_date": day.isoformat() if day is not None else None, "rows": rows}
    with _lock:
        if day is None:
            _live = (time_mod.monotonic(), packet)
        elif rows and day < cn_now().date():
            _history[day] = packet
            while len(_history) > HISTORY_MAX:
                _history.popitem(last=False)
    return packet


def reset_for_tests() -> None:
    global _live
    with _lock:
        _live = None
        _history.clear()
