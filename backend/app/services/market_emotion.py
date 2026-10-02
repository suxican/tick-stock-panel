"""Market sentiment from anonymous Kaipanla observations, with explicit provenance.

The client owns request caching and bounded concurrency. This orchestration never
reads local prices or invents a trading day when the supplier cannot prove one.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import date, datetime

from app.market_time import CN_TZ, cn_today
from app.services import kaipanla
from app.services.kaipanla_catalog import get_dataset, presets

_CURRENT = (
    "emotion", "capacity", "indices", "limit_counts", "limit_ladder_counts",
    "limit_performance", "withdrawal", "weight", "live",
)
_PREVIOUS = ("limit_counts", "limit_performance")
_LABELS = {config.id: {field.name: field.label for field in config.fields} for config in presets()}
_COUNTS = {
    "ztjs", "lbgd", "df_num", "SJZT", "SJDT", "first_board_count", "second_board_count",
    "third_board_count", "fourth_board_count", "fifth_plus_count", "limit_up_count",
    "two_board_count", "three_board_count", "max_board_count",
}
_MONEY = {"turnover", "main_net", "main_buy", "main_sell", "price"}
_NUMBERS = {"strong", "last_px", "increase_amount", "time", "Time"}
_OMIT = {"record_id", "date", "source", "fetched_at", "extra_json"}
_BREADTH_KEYS = {
    "up_count": "SZJS", "down_count": "XDJS", "limit_up_count": "ZT", "limit_down_count": "DT",
    "actual_limit_up_count": "SJZT", "actual_limit_down_count": "SJDT",
}
_MISSING = (
    ("independent_seal_rate", "独立封板率", "暂无已核验的独立封板率字段, 不以100减破板率代替。"),
    ("full_broken_list", "全板位今日炸板", "仅首板今日破板列表可用, 其他板位类型表示昨日连板未涨停个股。"),
    ("separate_promotion", "四板及高度板独立晋级率", "接口仅提供最高板合并口径, 不能拆成各高度独立晋级率。"),
    ("continuous_strength", "连板强度", "暂无已核验的独立连板强度口径, 不以连板高度代替。"),
    ("active_stock_trend", "活跃股走势", "暂无对应的匿名走势接口。"),
    ("capacity_intraday", "量能分钟曲线", "量能接口提供截面, 不包含已核验的分钟序列。"),
    ("index_intraday", "指数分时图", "现有开盘啦文档只提供指数快照, 未提供指数分钟序列接口。"),
    ("wind_vane", "风向标", "截图风向标缺少对应接口; 相关最强风口 GetFengKListBest 需登录。"),
    ("limit_down_list", "跌停列表", "暂无已核验的匿名跌停个股列表接口。"),
    ("auction_authenticated", "需认证竞价", "板块竞价及板块内股票竞价需登录, 竞价列表认证要求待确认。"),
)


def _unit(name: str) -> str:
    if name.endswith("_pct"):
        return "percent"
    if name.endswith("_wan"):
        return "wan"
    if name in _COUNTS:
        return "count"
    if name in _MONEY:
        return "yuan"
    return "number" if name in _NUMBERS else "text"


def _number(value, *, count: bool = False):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(number) or (count and (number < 0 or not number.is_integer())):
        return None
    return int(number) if number.is_integer() else number


def _columns(dataset_id: str) -> list[dict]:
    spec = get_dataset(dataset_id)
    return [
        {"name": name, "label": _LABELS[dataset_id].get(name, name), "unit": _unit(name)}
        for name in spec.columns if name not in _OMIT and not name.startswith("reserved")
    ]


def _section(name: str, day: date | None, *, state: str = "empty", message: str | None = None) -> dict:
    dataset_id = f"ext_kpl_{name}"
    out = {
        "id": dataset_id, "label": get_dataset(dataset_id).label, "state": state,
        "date": day.isoformat() if day else None, "fetched_at": None, "date_origin": None,
        "rows": [], "columns": _columns(dataset_id),
    }
    if message:
        out["message"] = message
    return out


def _rows(rows: list[dict], columns: list[dict]) -> list[dict]:
    return [{
        column["name"]: (
            str(row[column["name"]]) if row.get(column["name"]) is not None else None
        ) if column["unit"] == "text" else _number(
            row.get(column["name"]), count=column["unit"] == "count",
        ) for column in columns
    } for row in rows]


def _live_stocks(row: dict) -> list[dict]:
    stocks = {}
    for column, kind in (("Stock_json", "focus"), ("DisStock_json", "discussion")):
        try:
            entries = json.loads(row.get(column) or "[]")
        except (ValueError, TypeError):
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, list) or len(entry) < 2:
                continue
            code, name = entry[:2]
            if (not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code)
                    or not isinstance(name, str) or not name.strip()):
                continue
            change = _number(entry[2]) if len(entry) > 2 else None
            if code not in stocks:
                stocks[code] = {"symbol": code, "name": name.strip(), "change_pct": change, "kind": kind}
            elif stocks[code]["change_pct"] is None and change is not None:
                stocks[code]["change_pct"] = change
    return list(stocks.values())


def _from_result(name: str, day: date, result: dict) -> dict:
    out = _section(name, day)
    if result.get("state") not in {"ok", "empty"}:
        raise ValueError("unusable result")
    if result.get("data_date") not in (None, day.isoformat()):
        raise ValueError("wrong date")
    rows = result.get("rows")
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or row.get("date") not in (None, day.isoformat()) for row in rows
    ):
        raise ValueError("invalid rows")
    out.update(
        state="ok" if rows else "empty", rows=_rows(rows, out["columns"]),
        fetched_at=result.get("fetched_at"), date_origin=result.get("date_origin"),
    )
    if name == "live":
        for row in out["rows"]:
            row["stocks"] = _live_stocks(row)
    return out


async def _fetch(name: str, day: date, force: bool) -> dict:
    try:
        result = await kaipanla.fetch_dataset(f"ext_kpl_{name}", day, force=force)
        return _from_result(name, day, result)
    except Exception:
        return _section(name, day, state="error", message="该项开盘啦数据暂不可用。")


def _history(result: dict, through: date) -> dict:
    out = _section("emotion", None)
    out.update(label="情绪历史", date_origin="response_history", fetched_at=result.get("fetched_at"))
    out["columns"] = [{"name": "trade_date", "label": "交易日期", "unit": "text"}, *(
        column for column in out["columns"] if column["name"] in {"strong", "ztjs", "lbgd", "df_num"}
    )]
    raw = result.get("history", [])
    if not isinstance(raw, list) or len(raw) > 1000:
        raise ValueError("invalid history")
    sessions = {}
    for row in raw:
        day = date.fromisoformat(row["trade_date"])
        if day <= through:
            sessions[day] = _rows([row], out["columns"])[0]
    out["rows"] = [sessions[day] for day in sorted(sessions)[-60:]]
    if out["rows"]:
        out.update(state="ok", date=out["rows"][-1]["trade_date"])
    return out


def _breadth(live: dict, day: date | None) -> dict:
    out = {
        "id": "ext_kpl_live", "label": "涨跌分布(直播统计)", "state": live["state"],
        "date": day.isoformat() if day else None, "fetched_at": live["fetched_at"],
        "date_origin": live["date_origin"], "rows": [], "columns": [],
    }
    if live["state"] == "error":
        out["message"] = "直播统计暂不可用。"
        return out
    candidates = []
    for row in live["rows"]:
        try:
            timestamp = _number(row.get("Time"))
            published = datetime.fromtimestamp(timestamp, tz=CN_TZ)
            if published.date() != day:
                continue
            info = json.loads(row.get("ShareData_json") or "{}").get("ZDTJ_info")
            if not isinstance(info, dict):
                continue
            bins = [{"key": str(key), "label": f"档位 {key}", "count": _number(info.get(str(key)), count=True)}
                    for key in range(-10, 11)]
            counts = {name: _number(info.get(key), count=True) for name, key in _BREADTH_KEYS.items()}
            if any(bin_["count"] is None for bin_ in bins) or any(value is None for value in counts.values()):
                continue
            candidates.append({
                "source_time": timestamp, "published_at": published.isoformat(), "bins": bins, **counts,
            })
        except (AttributeError, ValueError, TypeError, OverflowError, OSError):
            continue
    if candidates:
        out.update(state="ok", rows=[max(candidates, key=lambda row: row["source_time"])])
        out["message"] = "来自该日直播统计; 原始百分数分档的桶边界及过滤范围未定义, 0%不代表已核验的平盘数。"
    else:
        out.update(state="empty", message="该日没有完整且发布时间可核验的直播涨跌分布。")
    return out


def _response(requested: date | None, day: date | None, previous: date | None,
              datasets: dict, history: dict) -> dict:
    current = [section for name, section in datasets.items() if not name.startswith("previous_")]
    relevant = [*current, history, *(datasets[f"previous_{name}"] for name in _PREVIOUS if previous)]
    has_current = any(section["state"] == "ok" and section["rows"] for section in current)
    if not has_current:
        state = "error" if any(section["state"] == "error" for section in current) else "empty"
    else:
        state = "ok" if all(section["state"] == "ok" for section in relevant) else "partial"
    fetched = [section["fetched_at"] for section in relevant if section.get("fetched_at")]
    return {
        "source": "开盘啦", "requested_date": requested.isoformat() if requested else None,
        "date": day.isoformat() if day else None, "previous_date": previous.isoformat() if previous else None,
        "fetched_at": max(fetched, default=None), "state": state, "datasets": datasets, "history": history,
        "missing": [{"id": key, "label": label, "reason": reason} for key, label, reason in _MISSING],
    }


async def get_market_emotion(target: date | None = None, *, force: bool = False) -> dict:
    """Resolve supplier sessions, then fetch the small dashboard set using its client cache."""
    through = target or cn_today()
    if through > cn_today():
        raise ValueError("不能查询未来日期")
    discovery = None
    try:
        discovery = await kaipanla.fetch_dataset("ext_kpl_emotion", through, force=force, include_history=True)
        history = _history(discovery, through)
    except Exception:
        history = _section("emotion", None, state="error", message="情绪历史暂不可用, 无法确认最近交易日期。")
        history["label"] = "情绪历史"
    day = target or (date.fromisoformat(history["date"]) if history["state"] == "ok" else None)
    datasets = {name: _section(name, day) for name in _CURRENT}
    previous = None
    known = [date.fromisoformat(row["trade_date"]) for row in history["rows"]]
    if day in known:
        previous = max((session for session in known if session < day), default=None)
    for name in _PREVIOUS:
        datasets[f"previous_{name}"] = _section(name, previous)
    if day is None:
        datasets["emotion"] = _section("emotion", None, state=history["state"], message="开盘啦尚未确认可用交易日期。")
    else:
        try:
            if target is None:
                row = next(row for row in history["rows"] if row["trade_date"] == day.isoformat())
                datasets["emotion"] = _from_result("emotion", day, {
                    "state": "ok", "rows": [row], "data_date": day.isoformat(),
                    "date_origin": "response", "fetched_at": history["fetched_at"],
                })
            else:
                datasets["emotion"] = _from_result("emotion", day, discovery or {})
        except Exception:
            datasets["emotion"] = _section("emotion", day, state="error", message="该日情绪数据暂不可用。")
        jobs = [(name, day) for name in _CURRENT if name != "emotion"]
        if previous:
            jobs.extend((f"previous_{name}", previous) for name in _PREVIOUS)
        results = await asyncio.gather(*(
            _fetch(name.removeprefix("previous_"), session, force) for name, session in jobs
        ))
        datasets.update((name, result) for (name, _), result in zip(jobs, results, strict=True))
    datasets["breadth"] = _breadth(datasets["live"], day)
    return _response(target, day, previous, datasets, history)
