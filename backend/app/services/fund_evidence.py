"""资金证据: 主力净额 / 龙虎榜 / 融资余额 / 北向持股, 只作证据展示, 不判定。

按「已加载且声明对应数据集的数据源」取数 (fund_flow / margin_balance / northbound_holding), 业务层
不写死厂商; 龙虎榜复用 dragon_tiger 服务 (兜底链、历史日落盘缓存)。

口径约束 (规划第 11.6 节决策 3):
- 每项带 source、unit、data_through (数据覆盖截止日)、available_at (本系统取到数据的时刻), 主力净额
  标注厂商模型口径。盘后/滞后数据不得在盘中当作当日: 5 日窗口只含已定版交易日; 融资 T-1、北向季度披露
  如实写出数据日期。
- 三态: ok / empty (查询成功但无记录) / unavailable (来源不可用或取数失败)。unavailable 与 empty 绝不
  输出为 0, 文案明示不能视为无资金流向。
- 不打分、不进入硬过滤、情景价位与条件判定; 本模块不被任何判定逻辑读取。
"""
from __future__ import annotations

import copy
import logging
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

from app.data_providers import custom as custom_sources
from app.market_time import CN_TZ
from app.services.huichun_calendar import BUNDLED_CALENDAR_PATH, load_market_calendar

logger = logging.getLogger(__name__)

FLOW_DATASET, MARGIN_DATASET, NORTH_DATASET = "fund_flow", "margin_balance", "northbound_holding"
WINDOW_DAYS = 5                      # 近 5 个已定版交易日
FINAL_AT = dtime(15, 30)             # 收盘后保守定版时点 (北京时间); 盘中/午休不含当日
MODEL_BASIS = "vendor_model"
UNAVAILABLE_TEXT = "资金数据不可用"
_HOLDING_TTL_S = 30 * 60
_NORTH_TTL_S = 12 * 3600
_INTRADAY_TTL_S = 4 * 60             # 厂商盘中约 4 分钟更新一次
_INTRADAY_STALE_S = 10 * 60          # 超过即显示不可用
_ITEMS = ("main_net", "dragon_tiger", "margin", "northbound")

_cache: dict[tuple, tuple[float, object]] = {}
_lock = threading.Lock()


def _source(dataset: str) -> str | None:
    for name in sorted(custom_sources.names()):
        try:
            if custom_sources.provider_has_dataset(name, dataset):
                return name
        except Exception:  # noqa: BLE001 -- 单个插件异常不影响其他来源
            continue
    return None


def _cached(key: tuple, ttl: float):
    with _lock:
        hit = _cache.get(key)
        return hit[1] if hit and time.monotonic() - hit[0] < ttl else None


def _store(key: tuple, value):
    with _lock:
        _cache[key] = (time.monotonic(), value)


def window_days(now: datetime) -> list[date] | None:
    """近 WINDOW_DAYS 个已定版交易日 (升序); 日历不可用返回 None。盘中与 15:30 前不含当日。"""
    try:
        days = load_market_calendar(BUNDLED_CALENDAR_PATH, start=now.date() - timedelta(days=30), end=now.date())
    except (ValueError, OSError):
        return None
    final = [d for d in days if d < now.date() or (d == now.date() and now.time() >= FINAL_AT)]
    return final[-WINDOW_DAYS:] if len(final) >= 1 else None


def _fetch_dragon(data_dir: Path | None, days: list[date]) -> dict[str, dict | None] | None:
    """{ISO 日: 龙虎榜载荷 | None(该日不可得)}; 无 data_dir 返回 None。"""
    if data_dir is None:
        return None
    from app.services import dragon_tiger

    out: dict[str, dict | None] = {}
    for day in days:
        try:
            payload = dragon_tiger.get_dragon_tiger(Path(data_dir), day)
        except Exception:  # noqa: BLE001 -- 龙虎榜只作证据, 失败标记该日不可得
            logger.warning("龙虎榜取数失败 %s", day, exc_info=True)
            payload = None
        ok = isinstance(payload, dict) and payload.get("state") == "ok" and payload.get("trade_date") == day.isoformat()
        out[day.isoformat()] = payload if ok else None
    return out


def fetch_holdings(symbols: list[str], now: datetime, data_dir: Path | None) -> dict | None:
    """持仓分析用的原始资金数据; 日历不可用返回 None (全部不可用)。每项来源失败只影响该项 (值为 None)。"""
    wanted = sorted({s for s in symbols if s})
    days = window_days(now)
    if days is None:
        return None
    key = ("holdings", tuple(wanted), days[-1], str(data_dir))
    hit = _cached(key, _HOLDING_TTL_S)
    if hit is not None:
        return hit
    fetched_at = now.isoformat()
    out: dict = {"days": [d.isoformat() for d in days], "fetched_at": fetched_at, "flow": None, "margin": None,
                 "north": {}, "dragon": None, "sources": {}}
    for field, dataset, method, args in (
            ("flow", FLOW_DATASET, "get_fund_flow", (wanted, days[0], days[-1])),
            ("margin", MARGIN_DATASET, "get_margin_balance", (wanted, days[-1] - timedelta(days=30), days[-1]))):
        name = _source(dataset)
        if name is None or not wanted:
            continue
        try:
            out[field] = getattr(custom_sources.get_provider(name), method)(*args)
            out["sources"][field] = name
        except Exception:  # noqa: BLE001 -- 资金证据失败不阻断研究, 标记该项不可用
            logger.warning("资金证据 %s 取数失败, 标记不可用", field, exc_info=True)
    north_name = _source(NORTH_DATASET)
    if north_name is not None:
        out["sources"]["north"] = north_name
        for symbol in wanted:
            nkey = ("north", north_name, symbol, days[-1])
            cached = _cached(nkey, _NORTH_TTL_S)
            if cached is not None:
                out["north"][symbol] = cached
                continue
            try:
                rows = custom_sources.get_provider(north_name).get_northbound(symbol, days[-1])
            except Exception:  # noqa: BLE001
                logger.warning("北向持股取数失败 %s, 标记不可用", symbol, exc_info=True)
                out["north"][symbol] = None
                continue
            _store(nkey, rows)
            out["north"][symbol] = rows
    out["dragon"] = _fetch_dragon(data_dir, days) if wanted else None
    _store(key, out)
    return out


def _item(status: str, source: str | None, unit: str | None, available_at: str | None, **extra) -> dict:
    return {"status": status, "source": source, "unit": unit, "available_at": available_at, **extra}


def _unavailable(source: str | None = None) -> dict:
    return _item("unavailable", source, None, None, data_through=None)


def _main_net(raw: dict | None, symbol: str) -> dict:
    if raw is None or raw.get("flow") is None:
        return _unavailable(((raw or {}).get("sources") or {}).get("flow"))
    source, window = raw["sources"].get("flow"), set(raw["days"])
    rows = [r for r in raw["flow"].get(symbol, []) if r.get("date") in window]
    base = {"source": source, "unit": "元", "available_at": raw["fetched_at"], "model_basis": MODEL_BASIS}
    if not rows:
        return {"status": "empty", **base, "data_through": None, "total": None, "days": [], "missing_days": raw["days"]}
    total = sum(r["main_net"] for r in rows)
    have = {r["date"] for r in rows}
    return {"status": "ok", **base, "data_through": rows[-1]["date"], "total": total,
            "direction": "inflow" if total > 0 else "outflow" if total < 0 else "flat",
            "days": [{"date": r["date"], "main_net": r["main_net"]} for r in rows],
            "missing_days": [d for d in raw["days"] if d not in have]}


def _dragon(raw: dict | None, symbol: str) -> dict:
    days = (raw or {}).get("dragon")
    if days is None:
        return _unavailable()
    got = {d: p for d, p in days.items() if p is not None}
    if not got:
        return _unavailable()
    sources = sorted({str(p.get("source")) for p in got.values() if p.get("source")})
    listings, seats = [], []
    for day, payload in sorted(got.items()):
        reasons, net = [], 0.0
        for item in (payload.get("all") or {}).get("stock_items") or []:
            if item.get("thscode") == symbol:
                if item.get("limit_reason"):
                    reasons.append(str(item["limit_reason"]))
                net += float(item.get("net_value") or 0.0)
        hit = any(i.get("thscode") == symbol for i in (payload.get("all") or {}).get("stock_items") or [])
        if hit:
            listings.append({"date": day, "reasons": reasons, "net_value": net, "source": payload.get("source")})
        for seat in (payload.get("hot_money") or {}).get("hot_money_items") or []:
            for row in seat.get("rows") or []:
                if row.get("thscode") == symbol:
                    seats.append({"date": day, "name": seat.get("name"),
                                  "net_value": row.get("hot_money_item_net_value")})
    status = "ok" if listings else "empty"
    basis = next((p.get("hot_money_basis") for p in got.values() if p.get("hot_money_basis")), None)
    return _item(status, "/".join(sources) or None, "元", raw["fetched_at"], data_through=max(got),
                 listings=listings, hot_money=seats, hot_money_basis=basis,
                 missing_days=[d for d in raw["days"] if d not in got])


def _margin(raw: dict | None, symbol: str) -> dict:
    if raw is None or raw.get("margin") is None:
        return _unavailable(((raw or {}).get("sources") or {}).get("margin"))
    rows = raw["margin"].get(symbol, [])
    base = {"source": raw["sources"].get("margin"), "unit": "元", "available_at": raw["fetched_at"]}
    if not rows:
        return {"status": "empty", **base, "data_through": None}
    latest = rows[-1]
    ref = rows[-1 - WINDOW_DAYS] if len(rows) > WINDOW_DAYS else None
    change = latest["financing_balance"] - ref["financing_balance"] if ref else None
    pct = change / ref["financing_balance"] if ref and ref["financing_balance"] else None
    return {"status": "ok", **base, "data_through": latest["date"], "balance": latest["financing_balance"],
            "compare_date": ref["date"] if ref else None, "change": change, "change_pct": pct}


def _northbound(raw: dict | None, symbol: str) -> dict:
    sources = (raw or {}).get("sources") or {}
    if raw is None or "north" not in sources or raw["north"].get(symbol) is None:
        return _unavailable(sources.get("north"))
    rows = raw["north"].get(symbol)
    base = {"source": raw["sources"]["north"], "unit": "股", "available_at": raw["fetched_at"], "disclosure": "quarterly"}
    if not rows:
        return {"status": "empty", **base, "data_through": None}
    latest, prev = rows[-1], rows[-2] if len(rows) > 1 else None
    ratio_change = (latest["ratio"] - prev["ratio"]) if prev and latest.get("ratio") is not None \
        and prev.get("ratio") is not None else None
    return {"status": "ok", **base, "data_through": latest["date"], "shares": latest["shares"],
            "ratio": latest.get("ratio"), "previous_date": prev["date"] if prev else None,
            "shares_change": latest["shares"] - prev["shares"] if prev else None, "ratio_change": ratio_change}


def summarize(raw: dict | None, symbol: str) -> dict:
    """单只持仓的资金证据; raw 为 None (日历不可用) 时四项均不可用。"""
    return {"model_basis": MODEL_BASIS, "window": raw["days"] if raw else [],
            "available_at": raw["fetched_at"] if raw else None,
            "main_net": _main_net(raw, symbol), "dragon_tiger": _dragon(raw, symbol),
            "margin": _margin(raw, symbol), "northbound": _northbound(raw, symbol)}


def _yi(value: float, *, signed: bool = True) -> str:
    sign = ("+" if value > 0 else "−" if value < 0 else "") if signed else ""
    v = abs(value)
    return f"{sign}{v / 1e8:.2f} 亿元" if v >= 1e8 else f"{sign}{v / 1e4:.0f} 万元"


def _md(day: str | None) -> str:
    return day[5:] if day else "未知"


def evidence_text(fund: dict) -> str:
    """资金证据的程序模板文字, 供报告依据与 AI 解读引用; 只描述事实, 不推断主力意图。"""
    parts = []
    m = fund["main_net"]
    if m["status"] == "ok":
        word = {"inflow": "净流入", "outflow": "净流出", "flat": "持平"}[m["direction"]]
        gap = f"，缺 {len(m['missing_days'])} 日" if m["missing_days"] else ""
        parts.append(f"近 {len(m['days'])} 个交易日主力净额合计 {_yi(m['total'])}（{word}，截至 {_md(m['data_through'])}{gap}）")
    elif m["status"] == "empty":
        parts.append("近 5 个交易日无主力净额记录")
    else:
        parts.append("主力净额：资金数据不可用，不能视为无资金流入或流出")
    d = fund["dragon_tiger"]
    if d["status"] == "ok":
        hot = f"，游资席位 {'、'.join(sorted({str(s['name']) for s in d['hot_money'] if s.get('name')}))}" if d["hot_money"] else ""
        parts.append(f"近 5 个交易日上龙虎榜 {len(d['listings'])} 次（{'、'.join(_md(x['date']) for x in d['listings'])}）{hot}")
    elif d["status"] == "empty":
        gap = f"（{len(d['missing_days'])} 日榜单未取到）" if d["missing_days"] else ""
        parts.append(f"近 5 个交易日未上龙虎榜{gap}")
    else:
        parts.append("龙虎榜：资金数据不可用，不能视为未上榜")
    g = fund["margin"]
    if g["status"] == "ok":
        move = (f"，较 {_md(g['compare_date'])} {_yi(g['change'])}" + (f"（{g['change_pct'] * 100:+.1f}%）" if g["change_pct"] is not None else "")
                if g["change"] is not None else "，比较基准不足")
        parts.append(f"融资余额 {_yi(g['balance'], signed=False)}{move}（数据日 {_md(g['data_through'])}，T-1）")
    elif g["status"] == "empty":
        parts.append("融资余额：非两融标的或无记录")
    else:
        parts.append("融资余额：资金数据不可用，不能视为无融资")
    n = fund["northbound"]
    if n["status"] == "ok":
        move = (f"，较上期 {n['ratio_change'] * 100:+.2f} 个百分点" if n["ratio_change"] is not None else "")
        ratio = f"占 A 股 {n['ratio'] * 100:.2f}%" if n["ratio"] is not None else "占比未知"
        parts.append(f"北向持股 {n['data_through']} {ratio}{move}（季度披露）")
    elif n["status"] == "empty":
        parts.append("北向持股：无记录")
    else:
        parts.append("北向持股：资金数据不可用，不能视为无北向持股")
    return "资金证据（猫爪，主力净额为厂商模型口径，仅作参考，不代表主力意图）：" + "；".join(parts)


def intraday_main_net(symbols: list[str], now: datetime) -> dict[str, dict]:
    """做T 用: 当日累计主力净额 {代码: 证据}; 厂商盘中约 4 分钟更新一次, 不是分钟级区间值。

    进程内按代码集合缓存 4 分钟; 刷新失败沿用上一份有效值, 超过 10 分钟未更新即不可用。
    返回值每项的 available_at 为本系统取到该数据的时刻。
    """
    wanted = sorted({s for s in symbols if s})
    out = {s: _unavailable() for s in wanted}
    name = _source(FLOW_DATASET)
    if not wanted or name is None:
        return out
    key = ("intraday", name, tuple(wanted))
    with _lock:
        hit = _cache.get(key)
    now_m = time.monotonic()
    if hit is None or now_m - hit[0] >= _INTRADAY_TTL_S:
        try:
            data = custom_sources.get_provider(name).get_fund_flow_now(wanted)
            hit = (now_m, {"data": data, "fetched_at": now.isoformat()})
            with _lock:
                _cache[key] = hit
        except Exception:  # noqa: BLE001 -- 失败沿用旧值 (带原取数时刻), 过期后不可用
            logger.warning("当日主力净额取数失败", exc_info=True)
    if hit is None or now_m - hit[0] >= _INTRADAY_STALE_S:
        return out
    payload = hit[1]
    for symbol in wanted:
        row = payload["data"].get(symbol)
        base = {"source": name, "unit": "元", "available_at": payload["fetched_at"], "model_basis": MODEL_BASIS,
                "kind": "intraday_cumulative", "refresh_hint": "厂商约 4 分钟更新一次，非分钟级区间值"}
        if row is None:
            out[symbol] = {"status": "empty", **base, "data_through": None}
        elif row.get("date") != now.date().isoformat():
            out[symbol] = _unavailable(name)  # 非当日数据不得当作当日
        else:
            out[symbol] = {"status": "ok", **base, "data_through": row["date"], "main_net": row["main_net"]}
    return out


def visible_at(evidence: dict | None, moment: datetime) -> dict | None:
    """回放/复盘读取: 只保留 available_at 不晚于 moment 的项, 其余按不可用处理 (避免未来函数)。"""
    if not isinstance(evidence, dict):
        return None

    def ok(item) -> bool:
        stamp = item.get("available_at") if isinstance(item, dict) else None
        try:
            return stamp is not None and datetime.fromisoformat(stamp).astimezone(CN_TZ) <= moment.astimezone(CN_TZ)
        except ValueError:
            return False

    if "status" in evidence:  # 单项 (做T)
        return copy.deepcopy(evidence) if ok(evidence) else _unavailable(evidence.get("source"))
    out = copy.deepcopy(evidence)
    for name in _ITEMS:
        if name in out and not ok(out[name]):
            out[name] = _unavailable(out[name].get("source") if isinstance(out[name], dict) else None)
    return out
