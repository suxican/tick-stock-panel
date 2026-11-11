"""AI 复盘数据快照 —— 给 LLM 的对比上下文 + 前端结构化面板的同一份数据。

一份快照两处用:
- 渲染为文本段拼进复盘 user prompt (近 5 日对比 / 情绪周期 / 连板梯队 / 涨停主线);
- 随 meta 事件下发并随报告归档, 前端据此画指标对比表、走势图、主线与梯队,
  AI 正文只写判断与计划, 不再搬运数字。

数据源(均为本地已落盘数据, 不触发远端拉取):
- regime_history: 逐日成交额/涨跌家数/涨跌停/炸板/封板率/最高连板/晋级率/情绪周期阶段。
  同一张表提供全部日期, 保证"今日 vs 昨日 vs 前 5 日均"是同一口径; 今日尚未入表
  (盘中或盘后管道未跑) 时用总览数据按相同字段补一行, 并标注 today_source=overview。
- 总览 overview: 复盘日涨停主线(唯一主线口径, 见 market_mainline.mainline_for_day)、
  连板梯队个股名单、板块涨幅榜(仅作异动参考, 标注是否属于主线)。
- mainline_history: 复盘日之前的逐日涨停主线排名, 只用于主线持续天数。
- enriched 缓存: 昨日涨停股今日表现 (缓存不覆盖时略过)。

所有分支失败都只降级为缺字段, 不影响复盘主流程。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any

import polars as pl

from app.market_time import cn_now

logger = logging.getLogger(__name__)

SNAPSHOT_VERSION = 1
SERIES_DAYS = 10          # 走势图天数(含复盘日)
AVG_DAYS = 5              # "前 N 日均" 的 N(不含复盘日)
MAINLINE_TOP = 5          # 涨停主线展示条数
MAINLINE_STREAK_RANK = 10  # 主线持续天数: 连续进入当日前 N 名的天数
_MARKET_CLOSE = time(15, 0)

# 指标定义: key, 标签, 单位, 小数位; 顺序即前端表格行序
METRIC_DEFS: list[tuple[str, str, str, int]] = [
    ("amount_yi", "成交额", "亿", 0),
    # 09:30–10:00 分钟K合计, 明日剧本"开盘半小时成交额"触发条件的同口径基准
    ("amount_30m", "开盘半小时成交额", "亿", 0),
    ("up_count", "上涨家数", "家", 0),
    ("limit_up", "涨停", "家", 0),
    ("limit_down", "跌停", "家", 0),
    ("broken", "炸板", "家", 0),
    ("seal_rate", "封板率", "%", 0),
    ("max_boards", "最高连板", "板", 0),
    ("promo_rate", "连板晋级率", "%", 0),
]

PHASE_LABELS = {
    "ice": "冰点", "ignite": "启动", "rally": "主升",
    "climax": "高潮", "ebb": "退潮", "repair": "修复",
}
STATE_LABELS = {
    "strong": "强势", "lean_strong": "偏强", "range": "震荡",
    "lean_weak": "偏弱", "weak": "弱势",
}


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


# ───────────────────────── 逐日序列 ─────────────────────────

def _regime_day(row: dict) -> dict:
    """regime_history 一行 → 快照日行(比例统一转为百分数)。"""
    seal = _num(row.get("seal_rate"))
    promo = _num(row.get("promo_rate"))
    amount = _num(row.get("total_amount"))
    return {
        "date": str(row.get("date")),
        "amount_yi": amount / 1e8 if amount is not None else None,
        "up_count": _num(row.get("up_count")),
        "down_count": _num(row.get("down_count")),
        "limit_up": _num(row.get("limit_up")),
        "limit_down": _num(row.get("limit_down")),
        "broken": _num(row.get("broken_limit")),
        "seal_rate": seal * 100 if seal is not None else None,
        "max_boards": _num(row.get("max_consecutive")),
        "promo_rate": promo * 100 if promo is not None else None,
        "phase": row.get("phase") or None,
        "state": row.get("state") or None,
    }


def _overview_day(overview: dict) -> dict:
    """regime 尚无复盘日时, 用总览按同字段补一行(晋级率/周期无法从总览得到)。"""
    b = overview.get("breadth") or {}
    lim = overview.get("limit") or {}
    amt = _num((overview.get("amount") or {}).get("total"))
    return {
        "date": str(overview.get("as_of")),
        "amount_yi": amt / 1e8 if amt else None,
        "up_count": _num(b.get("up")),
        "down_count": _num(b.get("down")),
        "limit_up": _num(lim.get("limit_up")),
        "limit_down": _num(lim.get("limit_down")),
        "broken": _num(lim.get("broken")),
        "seal_rate": _num(lim.get("seal_rate")),
        "max_boards": _num(lim.get("max_boards")),
        "promo_rate": None,
        "phase": None,
        "state": None,
    }


def _load_days(data_dir, as_of: date, overview: dict) -> tuple[list[dict], str]:
    """复盘日及之前最近 SERIES_DAYS 个交易日(升序) + 今日数据来源。"""
    days: list[dict] = []
    try:
        from app.services.regime_builder import load_regime_history

        df = load_regime_history(data_dir)
        if not df.is_empty() and "date" in df.columns:
            df = df.filter(pl.col("date") <= as_of).sort("date").tail(SERIES_DAYS)
            days = [_regime_day(r) for r in df.to_dicts()]
    except Exception as e:  # noqa: BLE001
        logger.debug("recap snapshot: regime history unavailable: %s", e)

    today_source = "regime"
    if not days or days[-1]["date"] != as_of.isoformat():
        days = (days + [_overview_day(overview)])[-SERIES_DAYS:]
        today_source = "overview"
    return days, today_source


def _build_metrics(days: list[dict]) -> list[dict]:
    today = days[-1] if days else {}
    prev = days[-2] if len(days) >= 2 else {}
    base = days[-(AVG_DAYS + 1):-1]
    out = []
    for key, label, unit, digits in METRIC_DEFS:
        vals = [d.get(key) for d in base if d.get(key) is not None]
        out.append({
            "key": key,
            "label": label,
            "unit": unit,
            "digits": digits,
            "today": today.get(key),
            "prev": prev.get(key),
            "avg5": sum(vals) / len(vals) if vals else None,
            "series": [d.get(key) for d in days],
        })
    return out


def _phase_info(days: list[dict]) -> dict | None:
    """最近一个有阶段标签的交易日的周期阶段 + 已持续天数。"""
    tagged = [d for d in days if d.get("phase")]
    if not tagged:
        return None
    last = tagged[-1]
    streak = 0
    for d in reversed(tagged):
        if d.get("phase") != last["phase"]:
            break
        streak += 1
    return {
        "key": last["phase"],
        "label": PHASE_LABELS.get(last["phase"], last["phase"]),
        "days": streak,
        # 窗口内全部同阶段: 实际持续可能更久, 前端/提示词显示为 "N+"
        "capped": streak == len(tagged) and len(days) >= SERIES_DAYS,
        "date": last["date"],
        "state_label": STATE_LABELS.get(last.get("state") or "", None),
    }


# ───────────────────────── 主线 / 梯队 ─────────────────────────

def _overview_mainlines(overview: dict) -> list[dict]:
    """总览的涨停主线(唯一主线来源, 按 rank 升序, 前 MAINLINE_STREAK_RANK 名)。"""
    items = (overview.get("mainlines") or {}).get("items") or []
    return sorted((m for m in items if m.get("member")), key=lambda m: int(m.get("rank") or 0))


def _mainlines(data_dir, as_of: date, overview: dict) -> list[dict]:
    """复盘日涨停主线前 N(取自总览, 与看板同源) + 连续进入前 MAINLINE_STREAK_RANK 的天数。

    持续天数: 复盘日本身计 1 天, 再向前数 mainline_history 中连续上榜的交易日。
    """
    items = _overview_mainlines(overview)
    if not items:
        return []
    top_by_day: dict = {}
    dates: list = []
    if data_dir is not None:
        try:
            from app.services.market_mainline import load_mainline_history

            df = load_mainline_history(data_dir, "concept")
            if not df.is_empty() and "date" in df.columns:
                df = df.filter(pl.col("date") < as_of)
                dates = sorted(df["date"].unique().to_list(), reverse=True)[:SERIES_DAYS - 1]
                top_by_day = {
                    d: set(df.filter((pl.col("date") == d) & (pl.col("rank") <= MAINLINE_STREAK_RANK))["member"].to_list())
                    for d in dates
                }
        except Exception as e:  # noqa: BLE001
            logger.debug("recap snapshot: mainline history unavailable: %s", e)
    out = []
    for r in items[:MAINLINE_TOP]:
        streak = 1
        for d in dates:
            if r["member"] not in top_by_day.get(d, set()):
                break
            streak += 1
        sym = r.get("leader_symbol")
        out.append({
            "name": r["member"],
            "rank": int(r.get("rank") or 0),
            "limit_up": int(r.get("limit_up_count") or 0),
            "max_boards": int(r.get("max_boards") or 0),
            "leader": {"symbol": sym, "name": r.get("leader_name") or sym} if sym else None,
            "streak": streak,
        })
    return out


def _sector_moves(overview: dict) -> dict:
    """板块涨幅榜(概念 + 行业, 成分股等权涨幅)速览; 进入涨停主线前列的标 mainline。

    涨幅榜只作异动参考, 主线判断以涨停主线为准。
    """
    mainline_names = {m["member"] for m in _overview_mainlines(overview)}

    def pick(kind_key: str, side: str, n: int) -> list[dict]:
        items = ((overview.get(kind_key) or {}).get(side) or [])[:n]
        return [{
            "name": it.get("name"),
            "pct": (_num(it.get("avg_pct")) or 0) * 100,
            "leader": (it.get("leader") or {}).get("name"),
            "mainline": it.get("name") in mainline_names,
        } for it in items]

    return {
        "leading": pick("concept_rank", "leading", 3) + pick("industry_rank", "leading", 3),
        "lagging": pick("concept_rank", "lagging", 2) + pick("industry_rank", "lagging", 2),
    }


def _ladder(overview: dict) -> list[dict]:
    tiers = (overview.get("limit") or {}).get("tiers") or []
    return [{
        "boards": int(t.get("boards") or 0),
        "count": int(t.get("count") or 0),
        "stocks": [{"symbol": s.get("symbol"), "name": s.get("name")} for s in (t.get("stocks") or [])[:5]],
    } for t in tiers]


def _prev_limit_up_perf(repo, as_of: date, prev_date: str | None) -> dict | None:
    """昨日涨停股今日平均涨幅与红盘率(enriched 缓存覆盖两日时才计算)。"""
    if not prev_date:
        return None
    getter = getattr(repo, "get_enriched_range", None)
    if getter is None:
        return None
    try:
        prev = date.fromisoformat(prev_date)
        df = getter(prev, as_of, columns=["symbol", "date", "change_pct", "signal_limit_up"])
        if df is None or df.is_empty():
            return None
        pool = df.filter((pl.col("date") == prev) & pl.col("signal_limit_up").fill_null(False))["symbol"]
        today = df.filter((pl.col("date") == as_of) & pl.col("symbol").is_in(pool))
        pct = today["change_pct"].drop_nulls()
        if pct.len() == 0:
            return None
        return {
            "count": int(pct.len()),
            "avg_pct": float(pct.mean()) * 100,
            "up_ratio": float((pct > 0).sum()) / pct.len() * 100,
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("recap snapshot: prev limit-up perf skipped: %s", e)
        return None


# ───────────────────────── 入口 ─────────────────────────

def is_intraday_snapshot(as_of: date, now: datetime | None = None) -> bool:
    """复盘日为今天且尚未收盘 → 盘中快照(数据非收盘口径)。"""
    now = now or cn_now()
    return as_of == now.date() and now.time() < _MARKET_CLOSE


def build_recap_snapshot(repo, overview: dict, *, now: datetime | None = None) -> dict | None:
    """构建复盘快照; overview 无 as_of 时返回 None。"""
    as_of_str = overview.get("as_of")
    if not as_of_str:
        return None
    as_of = date.fromisoformat(str(as_of_str))
    data_dir = getattr(getattr(repo, "store", None), "data_dir", None)
    now = now or cn_now()

    days, today_source = _load_days(data_dir, as_of, overview) if data_dir else ([_overview_day(overview)], "overview")
    if data_dir:
        from app.services.market_recap_plan import opening_amount_yi

        for d in days:
            traded = int((d.get("up_count") or 0) + (d.get("down_count") or 0))
            d["amount_30m"] = opening_amount_yi(data_dir, d["date"], traded)
    prev_date = days[-2]["date"] if len(days) >= 2 else None
    return {
        "version": SNAPSHOT_VERSION,
        "as_of": as_of.isoformat(),
        "intraday": is_intraday_snapshot(as_of, now),
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "today_source": today_source,
        "phase": _phase_info(days),
        "metrics": _build_metrics(days),
        "dates": [d["date"] for d in days],
        "mainlines": _mainlines(data_dir, as_of, overview),
        "mainline_source": (overview.get("mainlines") or {}).get("source"),
        "sectors": _sector_moves(overview),
        "ladder": _ladder(overview),
        "prev_limit_up": _prev_limit_up_perf(repo, as_of, prev_date),
    }


# ───────────────────────── prompt 文本 ─────────────────────────

def _fmt(v: float | None, digits: int = 0, unit: str = "") -> str:
    if v is None:
        return "—"
    return f"{v:.{digits}f}{unit}"


def _chg(today: float | None, base: float | None, unit: str) -> str:
    """变化描述: 百分比类给差值(百分点), 连板高度给差值(板), 其余给相对变化。"""
    if today is None or base is None:
        return "—"
    if unit == "%":
        return f"{today - base:+.0f}pp"
    if unit == "板":
        return f"{today - base:+.0f}板"
    if base == 0:
        return f"{today - base:+.0f}"
    return f"{(today - base) / base * 100:+.0f}%"


def snapshot_prompt_block(snapshot: dict | None) -> str:
    """快照 → 复盘 user prompt 的文本段(表格 + 列表, 控制 token)。"""
    if not snapshot:
        return ""
    parts: list[str] = []
    if snapshot.get("intraday"):
        parts.append(f"**注意: 本次为盘中快照(生成于 {snapshot.get('generated_at')}), 数据非收盘口径, 判断均为暂定。**\n")

    metrics = snapshot.get("metrics") or []
    if metrics:
        src = "今日为实时总览口径" if snapshot.get("today_source") == "overview" else "同一环境时序口径"
        lines = [f"## 近期对比({src})", "| 指标 | 今日 | 昨日 | 前5日均 | 较昨日 | 较5日均 |", "|---|---|---|---|---|---|"]
        for m in metrics:
            unit, d = m.get("unit") or "", m.get("digits") or 0
            lines.append(
                f"| {m['label']} | {_fmt(m.get('today'), d, unit)} | {_fmt(m.get('prev'), d, unit)} | "
                f"{_fmt(m.get('avg5'), d, unit)} | {_chg(m.get('today'), m.get('prev'), unit)} | "
                f"{_chg(m.get('today'), m.get('avg5'), unit)} |"
            )
        parts.append("\n".join(lines))

    phase = snapshot.get("phase")
    if phase:
        state = f", 综合状态 {phase['state_label']}" if phase.get("state_label") else ""
        stale = "" if phase.get("date") == snapshot.get("as_of") else f"(截至 {phase.get('date')}, 复盘日尚未计算)"
        days = f"{phase['days']}+" if phase.get("capped") else str(phase["days"])
        parts.append(f"## 情绪周期\n- 阶段: {phase['label']}, 已持续 {days} 个交易日{state}{stale}")

    ladder = snapshot.get("ladder") or []
    if ladder:
        lines = ["## 连板梯队"]
        for t in ladder[:6]:
            names = "、".join(s.get("name") or s.get("symbol") or "" for s in t.get("stocks") or [])
            more = f" 等{t['count']}家" if t["count"] > len(t.get("stocks") or []) else ""
            lines.append(f"- {t['boards']}板 ×{t['count']}: {names}{more}" if t["boards"] >= 2 else f"- 首板 ×{t['count']}")
        perf = snapshot.get("prev_limit_up")
        if perf:
            lines.append(f"- 昨日涨停股今日: 平均 {perf['avg_pct']:+.2f}%, 红盘率 {perf['up_ratio']:.0f}%(样本 {perf['count']} 只)")
        parts.append("\n".join(lines))

    mainlines = snapshot.get("mainlines") or []
    if mainlines:
        live = "; 复盘日主线尚未盘后入库, 按实时涨停数据同公式补算" if snapshot.get("mainline_source") == "live" else ""
        lines = [f"## 涨停主线(唯一主线口径, 按涨停家数与梯队评分排名{live})"]
        for m in mainlines:
            leader = (m.get("leader") or {}).get("name") or "—"
            lines.append(
                f"{m['rank']}. {m['name']}: 涨停 {m['limit_up']} 家, 最高 {m['max_boards']} 板, "
                f"龙头 {leader}, 连续 {m['streak']} 日位列前{MAINLINE_STREAK_RANK}"
            )
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
