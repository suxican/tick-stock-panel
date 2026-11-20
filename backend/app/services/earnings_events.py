"""业绩事件 (预告 / 快报 / 定期报告披露计划): 持仓分析的近期事件证据。

按「已加载且声明 earnings_events 数据集」的数据源取数, 业务层不写死厂商。只取 as_of 当时已披露的
版本。来源不可用返回 None, 页面显示「来源不可用」, 不当作无事件。只列已发布事实与日期, 不做盈利
预测, 不进入风险确认, 不改变研究结论。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date

from app.data_providers import custom as custom_sources

logger = logging.getLogger(__name__)

DATASET = "earnings_events"
LOOKBACK_DAYS = 120  # 与数据源约定的公告回看天数一致 (见插件实现)
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


def fetch_events(symbols: list[str], as_of: date) -> dict | None:
    """{代码: {forecast, express, disclosures}}; 无来源或取数失败返回 None (不可用)。"""
    wanted = sorted({s for s in symbols if s})
    name = _source()
    if not wanted or name is None:
        return None if name is None else {}
    key = (name, as_of, tuple(wanted))
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
            return hit[1]
    try:
        result = custom_sources.get_provider(name).get_earnings_events(wanted, as_of)
    except Exception:  # noqa: BLE001 -- 业绩事件只作证据, 失败不阻断研究, 标记不可用
        logger.warning("业绩事件取数失败, 标记来源不可用", exc_info=True)
        return None
    with _lock:
        _cache[key] = (time.monotonic(), result)
    return result


def summarize(raw: dict | None, today: date, horizon_end: date | None) -> dict:
    """单只股票的近期事件摘要; raw 为 None 时为不可用。"""
    if raw is None:
        return {"available": False}
    upcoming = sorted((d for d in raw.get("disclosures") or []
                       if not d.get("actual_date") and d.get("scheduled_date") and d["scheduled_date"] >= today.isoformat()),
                      key=lambda d: d["scheduled_date"])
    nxt = {"report_period": upcoming[0]["report_period"], "date": upcoming[0]["scheduled_date"]} if upcoming else None
    in_horizon = bool(nxt and horizon_end and nxt["date"] <= horizon_end.isoformat())
    return {"available": True, "forecast": raw.get("forecast"), "express": raw.get("express"),
            "next_disclosure": nxt, "disclosure_in_horizon": in_horizon}


def _pct(value) -> str:
    return f"{value * 100:+.0f}%"


def event_evidence(summary: dict, today: date) -> str:
    """近期事件的程序模板文字, 供报告依据与 AI 解读引用 (模型不得据此推断走势)。"""
    if not summary.get("available"):
        return "近期事件：业绩事件来源不可用，不能视为无事件"
    parts = []
    f = summary.get("forecast")
    if f:
        change = (f"，净利润同比 {_pct(f['change_min'])}～{_pct(f['change_max'])}"
                  if f.get("change_min") is not None and f.get("change_max") is not None else "")
        parts.append(f"业绩预告 {f['report_period']} 报告期 {f.get('type') or '类型未知'}{change}（{f['announce_date']} 公告）")
    else:
        parts.append(f"近 {LOOKBACK_DAYS} 天无业绩预告")
    e = summary.get("express")
    if e:
        change = f" 净利润同比 {_pct(e['net_profit_yoy'])}" if e.get("net_profit_yoy") is not None else ""
        parts.append(f"业绩快报 {e['report_period']} 报告期{change}（{e['announce_date']} 公告）")
    else:
        parts.append(f"近 {LOOKBACK_DAYS} 天无业绩快报")
    n = summary.get("next_disclosure")
    if n:
        parts.append(f"下一次定期报告 {n['report_period']} 报告期 预约 {n['date']} 披露"
                     + ("，在计划持有期内" if summary.get("disclosure_in_horizon") else ""))
    else:
        parts.append("下一次定期报告预约披露日未公布")
    return f"近期事件（截至 {today.isoformat()}，数据源已披露版本）：" + "；".join(parts)
