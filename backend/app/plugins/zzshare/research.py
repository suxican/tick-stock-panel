"""研究用 HTTP 接口适配。仅透出已经核实的字段，调用方不接触厂商结构。"""
from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import date, datetime, time

from app.market_time import CN_TZ
from app.plugins.zzshare import ZzshareError

_GET = Callable[[str, dict], object]
_PLATES = {"plates_list", "plates_rank", "plates_stocks"}
_LHB = {"lhb_list", "lhb_detail", "lhb_stock_history"}
_CODE = re.compile(r"[0-9]{6}")


def _rows(payload: object) -> list[dict]:
    if not isinstance(payload, list) or any(not isinstance(r, dict) for r in payload):
        raise ZzshareError("zzshare 研究数据缺少记录列表")
    return payload


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("文本字段无效")
    return value.strip()


def _number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("数值字段无效")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("数值字段无效")
    return result


def _day(value: str) -> str:
    return date.fromisoformat(value).isoformat()


def _timestamp(value: str, *, timespec: str = "seconds") -> str:
    if not isinstance(value, str) or len(value) < 16 or value[10] not in ("T", " "):
        raise ValueError("源更新时间缺少时刻")
    stamp = datetime.fromisoformat(value)
    stamp = stamp.replace(tzinfo=CN_TZ) if stamp.tzinfo is None else stamp.astimezone(CN_TZ)
    return stamp.isoformat(timespec=timespec)


def _rank(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("榜单名次无效")
    return value


def _stock(code: str) -> dict:
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise ValueError("股票代码无效")
    symbol = None
    if code.startswith(("60", "68")):
        symbol = code + ".SH"
    elif code.startswith(("00", "30")):
        symbol = code + ".SZ"
    elif code.startswith(("4", "8", "92")):
        symbol = code + ".BJ"
    return {"stock_code": code, "symbol": symbol}


def _plate_rows(get: _GET, kind: str, day: str, plate_type: int, code: str | None, limit: int) -> list[dict]:
    base = f"/v3/market/plates/{plate_type}"
    if kind == "plates_stocks":
        # 该端点给出带入选/退出日的成员；stocks/rank 是人气榜，并非完整分母。
        raw = _rows(get(f"{base}/{code}/stocks", {"date": day.replace("-", "")}))
    elif kind == "plates_rank":
        raw = _rows(get(base + "/rank", {"date1": day.replace("-", ""), "limit": limit}))
    else:
        raw = _rows(get(base, {}))  # 目录无历史日期参数。
    result = []
    seen = set()
    for r in raw:
        if r["plate_type"] != plate_type:
            raise ValueError("板块类别不符")
        plate = {"plate_type": plate_type, "plate_code": _text(r["plate_code"])}
        if kind == "plates_stocks":
            entered = _day(r["time_in"])
            exited = _day(r["time_out"]) if r.get("time_out") else None
            if plate["plate_code"] != code or entered > day or (exited and exited < day):
                raise ValueError("板块成分或有效日期不符")
            row = {**plate, **_stock(r["stock_code"]), "stock_name": _text(r["stock_name"]),
                   "trade_date": day, "time_in": entered, "time_out": exited}
            identity = row["stock_code"]
        else:
            row = {**plate, "plate_name": _text(r["plate_name"])}
            identity = row["plate_code"]
            if kind == "plates_list":
                row.update({k: _day(r[k]) if r.get(k) else None for k in ("date_in", "date_out")})
            else:
                stamp = _timestamp(r["time"])
                if _day(r["date1"]) != day or stamp[:10] != day:
                    raise ValueError("板块榜日期不符")
                row.update(trade_date=day, source_score=_number(r["score"]), change_pct=_number(r["rate"]),
                           net_flow_yuan=_number(r["money_leader"]), amount_yuan=_number(r["trade_money"]),
                           updated_at=stamp)
        if identity in seen:
            raise ValueError("板块数据重复")
        seen.add(identity)
        result.append(row)
    return result[:limit] if kind == "plates_rank" else result


def _sentiment(get: _GET, day: str, start: str) -> list[dict]:
    raw = _rows(get("/v3/market/sentiment/0/kline", {"date1": start.replace("-", ""),
                                                     "date2": day.replace("-", "")}))
    result = []
    for r in raw:
        d = _day(r["date"])
        if not start <= d <= day or r["modal_id"] != 0:
            raise ValueError("情绪指数日期或类型不符")
        result.append({"trade_date": d, "open": _number(r["p_open"]), "high": _number(r["p_high"]),
                       "low": _number(r["p_low"]), "close": _number(r["p_close"]),
                       "previous_close": _number(r["p_close_pre1d"])})
    return sorted(result, key=lambda r: r["trade_date"])


def _trend(get: _GET, day: str, model: int) -> list[dict]:
    data = get(f"/v3/api/sentiment/trend/{model}", {"date1": day})
    if not isinstance(data, list):
        raise ValueError("分时情绪数据无效")
    result = []
    for point in data:
        if not isinstance(point, list) or len(point) != 3:
            raise ValueError("分时情绪点格式无效")
        clock = time.fromisoformat(point[0])
        if clock.tzinfo is not None:
            raise ValueError("分时情绪时间应为北京时间")
        result.append({"trade_date": day, "time": clock.isoformat(),
                       "value": _number(point[1]), "aux_value": _number(point[2])})
    return sorted(result, key=lambda r: r["time"])


def _hot(get: _GET, day: str, limit: int) -> list[dict]:
    raw = _rows(get("/v3/open/sentiment/media/ths2/top", {"date1": day.replace("-", ""), "top_n": limit}))
    result = []
    for r in raw:
        if _day(r["collect_date"]) != day:
            raise ValueError("热榜日期不符")
        stamp = _timestamp(r["update_time"])
        # collect_date 是榜单归属日，update_time 可能在次日。不能将异日附带行情当作当日涨跌幅。
        same_day = stamp[:10] == day
        result.append({**_stock(r["symbol_code"]), "stock_name": _text(r["symbol_name"]),
                       "trade_date": day, "rank": _rank(r["rank"]), "rank_change": _number(r["rank_diff"]),
                       "change_pct": _number(r["last_pct"]) if same_day else None,
                       "price_yuan": _number(r["last_price"]) if same_day else None,
                       "updated_at": stamp})
    return result[:limit]


def _lhb(get: _GET, kind: str, day: str, code: str | None, start: str | None, limit: int) -> list[dict]:
    # 龙虎榜 list/detail 必须传 ISO：紧凑日期在实测中返回成功的空结果。
    base = "/v3/market/lhb"
    if kind == "lhb_list":
        rows = _rows(get(base + "/list", {"date1": day}))
        return [{**_stock(r["stock_code"]), "stock_name": _text(r["stock_name"]), "trade_date": day,
                 "reason": _text(r["up_reason"]), "change_pct": _number(r["quote_change"]),
                 "net_buy_yuan": _number(r["buy_in"]), "amount_yuan": _number(r["turnover"])} for r in rows]
    identity = _stock(code)
    if kind == "lhb_stock_history":
        rows = _rows(get(base + "/stock/history", {"stock_code": code}))
        history = []
        for r in rows:
            event_date = _day(r["date"])
            if event_date <= day and (not start or event_date >= start):
                history.append({**identity, "trade_date": event_date, "change_pct": _number(r["quote_change"]),
                                "net_buy_yuan": _number(r["buy_in"])})
        return sorted(history, key=lambda r: r["trade_date"], reverse=True)[:limit]
    data = get(base + "/detail", {"date1": day, "stock_code": code})
    if data is None:
        return []  # 成功业务信封 + null 表示该日无此股票的上榜记录。
    if not isinstance(data, dict) or not isinstance(data.get("detail"), dict):
        raise ValueError("龙虎榜明细结构无效")
    detail = data["detail"]
    if detail["stock_code"] != code:
        raise ValueError("龙虎榜明细股票不符")
    sides = {1: "buy", 2: "sell"}
    return [{**identity, "trade_date": day, "side": sides[r["type"]], "rank": _rank(r["rank"]),
             "reason_type": str(r["reason_type"]) if r.get("reason_type") is not None else None,
             "seat_name": _text(r["trader_name"]), "buy_yuan": _number(r["buy_amount"]),
             "sell_yuan": _number(r["sell_amount"]), "reason": _text(detail["up_reason"])}
            for r in _rows(data["traders"])]


def fetch_research(get: _GET, kind: str, day: str, *, code: str | None, plate_type: int,
                   limit: int, model: int, start: str | None) -> list[dict]:
    if day != _day(day) or (start is not None and (start != _day(start) or start > day)):
        raise ValueError("请输入有效 ISO 日期区间")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("条数范围为 1 至 1000")
    if plate_type not in (14, 15, 17) or type(model) is not int or model < 0:
        raise ValueError("板块类型或情绪模型无效")
    if kind == "plates_stocks" and not re.fullmatch(r"[a-zA-Z0-9]{1,16}", code or ""):
        raise ValueError("板块代码无效")
    if kind in {"lhb_detail", "lhb_stock_history"}:
        raw = (code or "").strip().upper()
        code = raw.partition(".")[0]
        ident = _stock(code)
        if raw != code and raw != ident["symbol"]:
            raise ValueError("股票交易所后缀不符")
    try:
        if kind in _PLATES:
            return _plate_rows(get, kind, day, plate_type, code, limit)
        if kind == "market_sentiment":
            return _sentiment(get, day, start or day)
        if kind == "sentiment_trend":
            return _trend(get, day, model)
        if kind == "ths_hot_top":
            return _hot(get, day, limit)
        if kind in _LHB:
            return _lhb(get, kind, day, code, start, limit)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ZzshareError(f"zzshare {kind} 返回的字段或日期无效") from exc
    raise ValueError("未知研究数据集")
