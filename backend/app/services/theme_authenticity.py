"""精选板块正宗度: 持仓所在的「最正宗」精选板块, 作为持仓分析证据。

按「已加载且声明 theme_authenticity 数据集」的数据源取数, 业务层不写死厂商。名单只有当前快照,
只用于当下的研究证据 (历史靠盘后留档)。来源不可用返回 None, 页面显示「来源不可用」, 不当作
「不正宗」。精选板块不在本地板块目录, 不改变主要关联板块、结论与价位。
"""
from __future__ import annotations

import logging
import threading
import time

from app.data_providers import custom as custom_sources

logger = logging.getLogger(__name__)

DATASET = "theme_authenticity"
_CACHE_TTL_S = 30 * 60
_cache: dict[tuple, tuple[float, dict]] = {}
_lock = threading.Lock()


def _source() -> str | None:
    for name in sorted(custom_sources.names()):
        try:
            if custom_sources.provider_has_dataset(name, DATASET):
                return name
        except Exception:  # noqa: BLE001 -- 单个插件异常不影响其他来源
            continue
    return None


def fetch_authenticity(symbols: list[str]) -> dict | None:
    """{代码: [{theme_id, theme_name, change_pct}]}; 无来源或取数失败返回 None (不可用)。"""
    wanted = sorted({s for s in symbols if s})
    name = _source()
    if name is None:
        return None
    if not wanted:
        return {}
    key = (name, tuple(wanted))
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
            return hit[1]
    try:
        result = custom_sources.get_provider(name).get_theme_authenticity(wanted)
    except Exception:  # noqa: BLE001 -- 正宗度只作证据, 失败不阻断研究, 标记不可用
        logger.warning("精选板块正宗度取数失败, 标记来源不可用", exc_info=True)
        return None
    with _lock:
        _cache[key] = (time.monotonic(), result)
    return result


def summarize(raw: dict | None, symbol: str) -> dict:
    if raw is None or symbol not in raw:
        return {"available": False}
    return {"available": True, "themes": list(raw[symbol])}


def authenticity_evidence(summary: dict) -> str:
    """正宗度的程序模板文字, 供报告依据与 AI 解读引用。"""
    if not summary.get("available"):
        return "精选板块正宗度：来源不可用，不能视为不正宗"
    themes = summary.get("themes") or []
    if not themes:
        return "精选板块正宗度（数据源当前名单）：未列入任何精选板块的最正宗名单"
    parts = []
    for theme in themes:
        change = theme.get("change_pct")
        day = f"当日 {change * 100:+.2f}%" if change is not None else "当日涨跌未知"
        parts.append(f"{theme.get('theme_name') or theme.get('theme_id')}（最正宗，{day}）")
    return "精选板块正宗度（数据源当前名单）：" + "；".join(parts)
