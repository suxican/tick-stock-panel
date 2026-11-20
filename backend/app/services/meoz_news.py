"""猫爪新闻 (news) 读取: 按 limit 分别缓存 60 秒, 不落盘。

只保留标题/链接/来源/摘要等展示字段, 不保存图片与正文; 不参与评分, 不接入 AI 上下文。
"""
from __future__ import annotations

import threading
import time as time_mod
from datetime import datetime

from app.data_providers import custom as custom_sources
from app.market_time import cn_now

TTL_S = 60.0
LIMIT_MIN, LIMIT_MAX = 1, 100

_lock = threading.Lock()
_cache: dict[int, tuple[float, dict]] = {}


class NewsUnavailableError(RuntimeError):
    """猫爪插件未加载或不支持新闻。"""


def _provider():
    try:
        provider = custom_sources.get_provider("meoz")
    except ValueError as exc:
        raise NewsUnavailableError("猫爪插件未就绪") from exc
    if not callable(getattr(provider, "get_news", None)):
        raise NewsUnavailableError("当前猫爪插件版本不支持新闻")
    return provider


def read(limit: int = 50) -> dict:
    """{fetched_at, rows, sources}; rows 按 display_at 倒序 (同时刻保持上游顺序), sources 按首次出现去重。"""
    if not LIMIT_MIN <= limit <= LIMIT_MAX:
        raise ValueError(f"limit 必须在 {LIMIT_MIN}-{LIMIT_MAX} 之间")
    with _lock:
        hit = _cache.get(limit)
    if hit and time_mod.monotonic() - hit[0] < TTL_S:
        return hit[1]
    rows = _provider().get_news(limit)
    # display_at 均为带 +08:00 的 ISO 串, 解析后比较; sorted 稳定, 同时刻保持上游顺序
    ordered = sorted(rows, key=lambda r: datetime.fromisoformat(r["display_at"]), reverse=True)
    sources: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if row["source"] not in seen:
            seen.add(row["source"])
            sources.append({"source": row["source"], "source_name": row["source_name"]})
    packet = {"fetched_at": cn_now().isoformat(timespec="seconds"), "rows": ordered, "sources": sources}
    with _lock:
        _cache[limit] = (time_mod.monotonic(), packet)
    return packet


def reset_for_tests() -> None:
    with _lock:
        _cache.clear()
