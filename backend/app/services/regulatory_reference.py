"""监管风险参考: 交易所重点监控名单与严重异常波动预测 (读取猫爪盘后留档, 转为源中立契约)。

- 重点监控 (`point_monitor` 留档): 名单只有当前快照, 每日 15:40 留档。交易日 D 取 D 当天或上一
  交易日的留档, D 落在监控期 [startdate, enddate] 内即命中。更旧的留档视为不可用 (名单可能已变)。
- 严重异动预测 (`anomaly_forecast` 留档): 交易日 T 收盘后算出, 作用于下一交易日。交易日 D 只取
  上一交易日的留档。
- 留档文件不存在 → 该项为 None (不可用, 不能当作无风险); 0 行文件 → {} (当日无)。
返回按六位代码索引, 调用方用 symbol.split(".")[0] 查询, 不猜交易所后缀。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.services.huichun_calendar import BUNDLED_CALENDAR_PATH, load_market_calendar

logger = logging.getLogger(__name__)

ROOT = ("meoz_archive", "v1")


def _previous_session(day: date) -> date | None:
    try:
        days = load_market_calendar(BUNDLED_CALENDAR_PATH, start=day - timedelta(days=20), end=day - timedelta(days=1))
    except (ValueError, OSError):
        return None
    return days[-1] if days else None


def _read(data_dir: Path, dataset: str, day: date) -> pl.DataFrame | None:
    path = Path(data_dir).joinpath(*ROOT, dataset, f"date={day.isoformat()}.parquet")
    if not path.exists():
        return None
    try:
        return pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError):
        logger.warning("%s %s 留档读取失败, 视为不可用", dataset, day, exc_info=True)
        return None


def _ymd(value) -> str | None:
    text = str(value or "").strip()
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if len(text) == 8 and text.isdigit() else None


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _monitor(frame: pl.DataFrame, day: date) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in frame.iter_rows(named=True):
        code = str(row.get("symbol") or "").strip()[:6]
        start, end = _ymd(row.get("startdate")), _ymd(row.get("enddate"))
        if not code or start is None or end is None or not start <= day.isoformat() <= end:
            continue
        out.setdefault(code, []).append({"label": row.get("type") or "交易监管", "start": start, "end": end,
                                         "reason": row.get("reason"), "name": row.get("name")})
    return out


def _anomaly(frame: pl.DataFrame) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in frame.iter_rows(named=True):
        code = str(row.get("symbol") or "").strip()[:6]
        if not code:
            continue
        required, deviation = _float(row.get("required_pct_chg")), _float(row.get("deviation_pct"))
        elapsed = _float(row.get("elapsed_days"))
        out[code] = {"rule": row.get("rule"), "trigger_price": _float(row.get("trigger_price")),
                     "required_pct": required / 100 if required is not None else None,  # 百分数 → 小数制
                     "status": row.get("status"), "name": row.get("name"),
                     "elapsed_days": int(elapsed) if elapsed is not None else None,
                     "deviation_pct": deviation / 100 if deviation is not None else None}
    return out


def regulatory_reference(day: date, data_dir: Path | None) -> dict:
    """交易日 day 适用的重点监控与严重异动预测; 不可用的项为 None。"""
    out = {"monitor": None, "anomaly": None, "monitor_as_of": None, "anomaly_as_of": None}
    if data_dir is None:
        return out
    previous = _previous_session(day)
    for candidate in (day, previous):
        if candidate is None:
            continue
        frame = _read(data_dir, "point_monitor", candidate)
        if frame is not None:
            out["monitor"], out["monitor_as_of"] = _monitor(frame, day), candidate.isoformat()
            break
    if previous is not None:
        frame = _read(data_dir, "anomaly_forecast", previous)
        if frame is not None:
            out["anomaly"], out["anomaly_as_of"] = _anomaly(frame), previous.isoformat()
    return out


_ANOMALY_STATUS = {"reachable": "涨跌停内可触及", "unreachable": "涨跌停内不可触及"}


def risk_items(symbol, regulatory):
    """重点监控与严重异动预测的风险项; 来源不可用时为"不可用"项, 不当作无风险。"""
    regulatory = regulatory or {}
    code = str(symbol).split(".")[0]
    items = []
    monitor, anomaly = regulatory.get("monitor"), regulatory.get("anomaly")
    if monitor is None:
        items.append({"id": "monitor_unknown", "label": "监管名单不可用", "detail": "无法确认是否被交易所重点监控",
                      "source": None})
    elif code in monitor:
        entries = monitor[code]
        detail = "；".join(f"{e['label']} {e['start']} 至 {e['end']}"
                          + (f"：{str(e['reason'])[:40]}" if e.get("reason") else "") for e in entries)
        items.append({"id": "monitored", "label": "交易所重点监控", "detail": detail, "source": "exchange_daily"})
    if anomaly is None:
        items.append({"id": "anomaly_unknown", "label": "严重异动预测不可用", "detail": "无法确认是否接近严重异常波动",
                      "source": None})
    elif code in anomaly:
        a = anomaly[code]
        parts = [str(a.get("rule") or "严重异常波动")]
        if a.get("trigger_price") is not None:
            parts.append(f"触发价 {a['trigger_price']:.2f}")
        if a.get("required_pct") is not None:
            parts.append(f"需涨跌 {a['required_pct'] * 100:+.2f}%")
        if a.get("status"):
            parts.append(_ANOMALY_STATUS.get(a["status"], str(a["status"])))
        items.append({"id": "anomaly_forecast", "label": "严重异动预测", "detail": "，".join(parts),
                      "source": "exchange_daily"})
    return items
def regulatory_listing(day: date, data_dir: Path | None, code_to_symbol: dict[str, str]) -> dict:
    """异动监控页「交易所口径」: 交易日 day 适用的严重异动预测与重点监控名单 (展开为行)。

    code_to_symbol 由证券目录给出六位代码 → 带后缀代码, 查不到时保留六位代码 (不猜交易所)。
    某项留档缺失为 None (不可用), 与空列表 (当日无) 区分。
    """
    ref = regulatory_reference(day, data_dir)
    anomaly = None if ref["anomaly"] is None else sorted(
        ({"symbol": code_to_symbol.get(code, code), "code": code, **v} for code, v in ref["anomaly"].items()),
        key=lambda r: (r["required_pct"] is None, abs(r["required_pct"] or 0)))
    monitor = None if ref["monitor"] is None else sorted(
        ({"symbol": code_to_symbol.get(code, code), "code": code, "name": e.get("name"), "label": e["label"],
          "start": e["start"], "end": e["end"], "reason": e.get("reason")}
         for code, entries in ref["monitor"].items() for e in entries),
        key=lambda r: (r["end"], r["code"]))
    return {"target_day": day.isoformat(), "anomaly": anomaly, "monitor": monitor,
            "anomaly_as_of": ref["anomaly_as_of"], "monitor_as_of": ref["monitor_as_of"]}
