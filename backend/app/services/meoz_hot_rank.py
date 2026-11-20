"""猫爪热榜实时读取: 个股热榜 (hotstock 三榜单) 与板块热搜 (theme_hot_kp)。

- 只实时读取, 进程内缓存 60 秒, 不落盘 (热搜每日盘后留档由 meoz_archive 负责)。
- 每个榜单独立成败: 失败标记 available=False (页面显示「来源不可用」), 成功但为空是真实的空榜。
- hotstock 实时接口没有时间戳, 证据时间只有 fetched_at; 热搜带上游 available_at (updated_at)。
- 只作展示证据, 不参与首板优先分、候选判定或主线评分。
"""
from __future__ import annotations

import logging
import threading
import time as time_mod

from app.data_providers import custom as custom_sources
from app.market_time import cn_now

logger = logging.getLogger(__name__)

LIVE_TTL_S = 60.0
STOCK_LISTS = ("ths_normal", "dfcf_renqi", "ths_trend")

_lock = threading.Lock()
_cache: tuple[float, dict] | None = None


class HotRankUnavailableError(RuntimeError):
    """猫爪插件未加载或不支持热榜。"""


def _provider():
    try:
        provider = custom_sources.get_provider("meoz")
    except ValueError as exc:
        raise HotRankUnavailableError("猫爪插件未就绪") from exc
    if not callable(getattr(provider, "get_hot_stocks", None)) or not callable(getattr(provider, "get_theme_hot", None)):
        raise HotRankUnavailableError("当前猫爪插件版本不支持热榜")
    return provider


def read() -> dict:
    """{fetched_at, stocks: {榜单: {available, rows}}, themes: {available, rows}}。"""
    global _cache
    with _lock:
        hit = _cache
    if hit and time_mod.monotonic() - hit[0] < LIVE_TTL_S:
        return hit[1]
    provider = _provider()
    stocks = {}
    for kind in STOCK_LISTS:
        try:
            stocks[kind] = {"available": True, "rows": provider.get_hot_stocks(kind)}
        except Exception:  # noqa: BLE001 -- 单个榜单失败不影响其余, 标记不可用
            logger.warning("猫爪热榜 %s 取数失败", kind, exc_info=True)
            stocks[kind] = {"available": False, "rows": []}
    try:
        themes = {"available": True, "rows": provider.get_theme_hot()}
    except Exception:  # noqa: BLE001
        logger.warning("猫爪板块热搜取数失败", exc_info=True)
        themes = {"available": False, "rows": []}
    packet = {"fetched_at": cn_now().isoformat(timespec="seconds"), "stocks": stocks, "themes": themes}
    with _lock:
        _cache = (time_mod.monotonic(), packet)
    return packet


def reset_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None
