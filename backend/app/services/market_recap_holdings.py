"""AI 复盘「持仓联动」—— 批次提醒持仓 × 当日盘面, 条件由程序给出, AI 只做解释。

个股层面的数字全部复用持仓研究引擎 holding_analysis.analyze_holdings
(盈亏按登记的参考成本原样计算、MA20/60 趋势、支撑/压力、相对所属板块强弱),
本模块只补两件事:
1. 市场联动: 所属板块是否在当日涨跌幅前列、是否在涨停主线(及连续天数);
2. 程序条件: 以引擎的原始价支撑/压力生成"收盘 ≥/≤ X"条件及含义。

两种模式:
- full(完整): 已登记建仓日且填写了失效条件、引擎研究条件齐备 → 条件含义沿用
  引擎的走强/走弱情景应对(含风控价);
- market(只看市场联动): 缺建仓日或失效条件 → 不判断持仓阶段, 条件只描述
  联动是否走强/失效, 不给仓位动作。

AI 拿到的是不含数量与成本金额的摘要, 被要求只解释, 不新增价位、数量或动作。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time

import polars as pl

from app.market_time import CN_TZ

logger = logging.getLogger(__name__)

MAX_ROWS = 30
_AFTER_CLOSE = time(15, 31)  # 晚于引擎收盘判定(15:30), 复盘日即完整交易日
_PNL_TEXT = {"deep_loss": "深套", "loss": "浮亏", "flat": "接近成本", "profit": "浮盈", "unknown": "未知"}
_TREND_TEXT = {"up": "多头排列", "down": "空头排列", "range": "震荡", "unknown": "未知"}
_STRENGTH_TEXT = {"stronger": "跑赢板块", "weaker": "跑输板块", "inline": "与板块同步", "unknown": "强弱未知"}
# 引擎降级时的提示里, 这几条在复盘(收盘后、只用日线)场景下属于预期行为, 不展示
_EXPECTED_WARNINGS = ("实时行情", "可卖数量未知", "无新鲜且带真实市场时间的报价")


def _leaf(name: str) -> str:
    """"通信设备 / 通信网络设备及器件" → "通信网络设备及器件"(与总览行业二级名对齐)。"""
    return str(name or "").split(" / ")[-1].strip()


def _sector_moves(snapshot: dict) -> dict[str, tuple[str, float]]:
    """快照里涨跌幅居前/居后的板块 → {名称: (leading|lagging, 涨跌幅%)}。"""
    out: dict[str, tuple[str, float]] = {}
    for side in ("leading", "lagging"):
        for item in (snapshot.get("sectors") or {}).get(side) or []:
            if item.get("name"):
                out[_leaf(item["name"])] = (side, float(item.get("pct") or 0))
    return out


def _mainline_ranks(data_dir, as_of: date, names: set[str], top: int = 10, days: int = 10) -> dict[str, dict]:
    """持仓所属概念在复盘日涨停主线中的名次与连续进入前 top 的天数。"""
    if not names:
        return {}
    try:
        from app.services.market_mainline import load_mainline_history

        df = load_mainline_history(data_dir, "concept")
    except Exception as e:  # noqa: BLE001
        logger.debug("holdings linkage: mainline history unavailable: %s", e)
        return {}
    if df.is_empty() or "date" not in df.columns:
        return {}
    dates = sorted(df.filter(pl.col("date") <= as_of)["date"].unique().to_list(), reverse=True)[:days]
    df = df.filter((pl.col("date") <= as_of) & (pl.col("rank") <= top) & pl.col("member").is_in(list(names)))
    if not dates or dates[0] != as_of:
        return {}
    by_day = {d: set(df.filter(pl.col("date") == d)["member"].to_list()) for d in dates}
    today = {r["member"]: int(r["rank"]) for r in df.filter(pl.col("date") == as_of).to_dicts()}
    out = {}
    for name, rank in today.items():
        streak = 0
        for d in dates:
            if name not in by_day[d]:
                break
            streak += 1
        out[name] = {"rank": rank, "streak": streak}
    return out


def _primary_sector(sectors: list[dict]) -> dict | None:
    """展示用所属方向: 优先有效的最细行业(含二级), 其次有效概念。"""
    valid = [s for s in sectors if s.get("valid") and s.get("change_ratio") is not None]
    industries = sorted((s for s in valid if s.get("kind") == "industry"),
                        key=lambda s: (-str(s.get("name") or "").count(" / "), str(s.get("key"))))
    return industries[0] if industries else (valid[0] if valid else None)


def _fmt_price(v: float) -> str:
    return f"{v:.2f}"


def _conditions(row: dict, mode: str) -> list[dict]:
    """程序条件: 收盘相对原始价支撑/压力。full 模式沿用引擎情景的应对与风控价。"""
    tech = row.get("technicals") or {}
    support, resistance = tech.get("support"), tech.get("resistance")
    out: list[dict] = []
    if mode == "full":
        for sc in (row.get("plan") or {}).get("scenarios") or []:
            trig = sc.get("price_trigger")
            if sc.get("key") in ("bull", "bear") and trig and trig.get("value"):
                out.append({"metric": "close", "op": trig["op"], "value": float(trig["value"]),
                            "label": f"收盘 {'≥' if trig['op'] == '>=' else '≤'} {_fmt_price(trig['value'])}",
                            "meaning": sc.get("action") or ""})
        if out:
            return out
    if resistance:
        out.append({"metric": "close", "op": ">=", "value": round(float(resistance), 2),
                    "label": f"收盘 ≥ {_fmt_price(resistance)}(压力)",
                    "meaning": "站上压力, 联动走强"})
    if support:
        out.append({"metric": "close", "op": "<=", "value": round(float(support), 2),
                    "label": f"收盘 ≤ {_fmt_price(support)}(支撑)",
                    "meaning": "跌破支撑, 联动失效"})
    return out


def _row(row: dict, moves: dict, mainlines: dict) -> dict:
    profile = row.get("profile") or {}
    has_basics = bool(row.get("opened_on")) and bool(str(profile.get("invalidation") or "").strip())
    can_adopt = bool((row.get("plan") or {}).get("can_adopt"))
    mode = "full" if has_basics and can_adopt else "market"
    missing = []
    if not row.get("opened_on"):
        missing.append("建仓日")
    if not str(profile.get("invalidation") or "").strip():
        missing.append("失效条件")

    sectors = row.get("sectors") or []
    primary = _primary_sector(sectors)
    # 市场联动: 所属板块在当日涨跌幅前列/居后; 所属概念在涨停主线
    hot = []
    for s in sectors:
        hit = moves.get(_leaf(s.get("name")))
        if hit:
            hot.append({"name": _leaf(s["name"]), "side": hit[0], "pct": hit[1]})
    mainline = None
    for s in sectors:
        if s.get("kind") == "concept" and s.get("name") in mainlines:
            m = mainlines[s["name"]]
            if mainline is None or m["rank"] < mainline["rank"]:
                mainline = {"name": s["name"], **m}

    price = row.get("price") or {}
    return {
        "symbol": row["symbol"],
        "name": row.get("name") or row["symbol"],
        "mode": mode,
        "missing": missing,
        "close": price.get("value"),
        "price_date": price.get("date"),
        "pnl_pct": None if row.get("pnl_ratio") is None else row["pnl_ratio"] * 100,
        "pnl_state": row.get("pnl_state") or "unknown",
        "age_days": row.get("age_days") if mode == "full" else None,
        "trend": row.get("trend") or "unknown",
        "relative": row.get("relative_strength") or "unknown",
        "stock_pct": row.get("_stock_change"),
        "sector": None if primary is None else {
            "name": _leaf(primary["name"]), "pct": primary["change_ratio"] * 100,
        },
        "hot": hot[:3],
        "mainline": mainline,
        "conditions": _conditions(row, mode),
        "warnings": [w for w in row.get("warnings") or [] if not any(k in w for k in _EXPECTED_WARNINGS)][:3],
    }


def _stock_changes(repo, symbols: list[str], as_of: date) -> dict[str, float]:
    """复盘日个股涨跌幅(%), 从 enriched 缓存取前一交易日收盘计算; 不可用返回空。"""
    getter = getattr(repo, "get_enriched_range", None)
    if getter is None or not symbols:
        return {}
    try:
        from datetime import timedelta

        df = getter(as_of - timedelta(days=14), as_of, symbols=symbols, columns=["symbol", "date", "close"])
        if df is None or df.is_empty():
            return {}
        out = {}
        for sym, g in df.sort("date").group_by("symbol"):
            g = g.filter(pl.col("date") <= as_of)
            if g.height >= 2 and g["date"][-1] == as_of and g["close"][-2]:
                out[sym[0] if isinstance(sym, tuple) else sym] = (g["close"][-1] / g["close"][-2] - 1) * 100
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("holdings linkage: stock change unavailable: %s", e)
        return {}


def build_holdings_linkage(repo, snapshot: dict | None, *, sector_service=None, quote_service=None,
                           now: datetime | None = None) -> dict | None:
    """批次提醒持仓 × 复盘日盘面。无批次或引擎不可用时返回 None(复盘照常)。"""
    if not snapshot:
        return None
    from app.services.holding_analysis import analyze_holdings
    from app.services.holding_analysis_store import HoldingAnalysisStore

    as_of = date.fromisoformat(snapshot["as_of"])
    data_dir = repo.store.data_dir
    # 盘后复盘固定按复盘日收盘研究(只用日线, 结果可复现); 盘中才用实时行情
    intraday = bool(snapshot.get("intraday"))
    research_now = now if intraday and now else datetime.combine(as_of, _AFTER_CLOSE, CN_TZ)
    try:
        profiles = {k: v["profile"] for k, v in HoldingAnalysisStore(data_dir).profiles().items()}
    except Exception as e:  # noqa: BLE001 - 研究设置不可读时按无设置处理(全部降级为市场联动)
        logger.info("holdings linkage: profiles unavailable: %s", e)
        profiles = {}
    try:
        report = analyze_holdings(repo, quote_service if intraday else None, sector_service, None,
                                  "lots", profiles=profiles, now=research_now)
    except Exception as e:  # noqa: BLE001
        logger.warning("holdings linkage: analysis failed: %s", e)
        return None
    raw_rows = [r for r in report.get("rows") or [] if r.get("asset_type") == "stock"][:MAX_ROWS]
    if not raw_rows:
        return None

    changes = _stock_changes(repo, [r["symbol"] for r in raw_rows], as_of)
    concept_names = {s["name"] for r in raw_rows for s in r.get("sectors") or [] if s.get("kind") == "concept"}
    mainlines = _mainline_ranks(data_dir, as_of, concept_names)
    moves = _sector_moves(snapshot)
    rows = []
    for r in raw_rows:
        r["_stock_change"] = changes.get(r["symbol"])
        rows.append(_row(r, moves, mainlines))
    # 排序: 与当日热点/主线相关的在前, 其次按浮亏深度
    rows.sort(key=lambda x: (not (x["hot"] or x["mainline"]), x["pnl_pct"] if x["pnl_pct"] is not None else 0))
    return {
        "as_of": report.get("as_of_date"),
        "rows": rows,
        "market_only": sum(r["mode"] == "market" for r in rows),
        "warnings": [w for w in report.get("warnings") or [] if not any(k in w for k in _EXPECTED_WARNINGS)][:3],
    }


def _raw_closes(repo, day: date, symbols: list[str]) -> dict[str, float] | None:
    """复盘日原始收盘价(与引擎支撑/压力同为原始价基准); 缓存不覆盖返回 None。"""
    getter = getattr(repo, "get_enriched_range", None)
    if getter is None or not symbols:
        return None
    try:
        df = getter(day, day, symbols=symbols, columns=["symbol", "date", "close", "raw_close"])
    except Exception as e:  # noqa: BLE001
        logger.debug("holdings check: closes unavailable: %s", e)
        return None
    if df is None:
        return None
    out = {}
    for r in df.filter(pl.col("date") == day).to_dicts():
        v = r.get("raw_close") if r.get("raw_close") is not None else r.get("close")
        if v is not None:
            out[r["symbol"]] = float(v)
    return out


def check_holding_conditions(prev_holdings: dict | None, snapshot: dict, repo, *, from_as_of: str) -> dict | None:
    """核验上一交易日为持仓给出的程序条件在复盘日是否触发。

    条件是触发位而不是预测, 结果为 triggered / not / pending(盘中或数据缺失),
    不计入剧本命中率。已从批次删除的股票照样核验(条件给出时仍持有)。
    """
    rows = [r for r in (prev_holdings or {}).get("rows") or [] if r.get("conditions")]
    if not rows:
        return None
    day = date.fromisoformat(snapshot["as_of"])
    intraday = bool(snapshot.get("intraday"))
    closes = None if intraday else _raw_closes(repo, day, [r["symbol"] for r in rows])
    ops = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, "<": lambda a, b: a < b}
    out = []
    for r in rows:
        close = None if closes is None else closes.get(r["symbol"])
        conds = []
        for c in r["conditions"]:
            op = ops.get(c.get("op"))
            if close is None or op is None:
                status = "pending"
            else:
                status = "triggered" if op(close, float(c["value"])) else "not"
            conds.append({"label": c.get("label"), "meaning": c.get("meaning"), "status": status})
        out.append({"symbol": r["symbol"], "name": r.get("name") or r["symbol"],
                    "close": close, "conditions": conds})
    return {"from_as_of": from_as_of, "as_of": snapshot["as_of"], "rows": out,
            "triggered": sum(c["status"] == "triggered" for r in out for c in r["conditions"])}


def holdings_check_prompt_block(check: dict | None) -> str:
    """昨日持仓条件今日触发情况 → 提示词(程序判定, 模型只解释)。"""
    if not check:
        return ""
    hits = [f"{r['name']} {c['label']} → {c['meaning']}(收盘 {r['close']:.2f})"
            for r in check["rows"] for c in r["conditions"] if c["status"] == "triggered"]
    pending = any(c["status"] == "pending" for r in check["rows"] for c in r["conditions"])
    lines = [f"## 昨日持仓条件今日触发(来自 {check['from_as_of']}, 程序判定)"]
    lines += [f"- {h}" for h in hits] or ["- 无条件触发"]
    if pending:
        lines.append("- 部分条件因数据未定型暂不能判定")
    return "\n".join(lines)


def holdings_prompt_block(holdings: dict | None) -> str:
    """持仓联动 → 提示词文本。不含数量与成本金额; 条件为程序结果, 模型不得改动。"""
    if not holdings or not holdings.get("rows"):
        return ""
    lines = ["## 持仓联动(程序计算; 条件与价位不得改动、不得新增)"]
    for r in holdings["rows"]:
        parts = [f"{r['name']}({r['symbol']})", "完整模式" if r["mode"] == "full" else "只看市场联动"]
        if r["sector"]:
            stock = f", 个股 {r['stock_pct']:+.2f}%" if r["stock_pct"] is not None else ""
            parts.append(f"所属 {r['sector']['name']} 今日 {r['sector']['pct']:+.2f}%{stock}, {_STRENGTH_TEXT[r['relative']]}")
        if r["hot"]:
            parts.append("今日" + "、".join(f"{h['name']}{'涨幅前列' if h['side'] == 'leading' else '跌幅居前'}" for h in r["hot"]))
        if r["mainline"]:
            parts.append(f"涨停主线 {r['mainline']['name']} 第{r['mainline']['rank']}名、连续{r['mainline']['streak']}日")
        if r["pnl_pct"] is not None:
            parts.append(f"相对登记成本 {r['pnl_pct']:+.1f}%({_PNL_TEXT[r['pnl_state']]})")
        parts.append(f"趋势 {_TREND_TEXT[r['trend']]}")
        if r["conditions"]:
            parts.append("条件: " + "; ".join(f"{c['label']} → {c['meaning']}" for c in r["conditions"]))
        lines.append("- " + " | ".join(parts))
    return "\n".join(lines)
