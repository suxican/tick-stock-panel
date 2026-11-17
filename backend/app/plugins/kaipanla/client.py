"""开盘啦接口客户端。调用方只接触标准化后的记录, 不接触厂商数组下标。

实测 (2026-11-17 盘中及 15:10, 见 docs/superpowers/specs/2026-11-16-kaipanla-intraday-design.md §10):
- 今日接口在 apphwhq, 历史接口在 apphis; 历史接口查当日返回 errcode 1020;
- ZhangTingExpression (晋级率) 今日地址返回占位垃圾, 只能事后经历史地址取;
- DailyLimitPerformance 行 [15] 为连板数 (文档未写明), [2]/[20] 含义不明不使用;
- 单请求 330–2500 ms, 1 秒间隔下未见限流。
"""

from __future__ import annotations

import math
import time
from datetime import date, datetime
from threading import Lock

import httpx

from app.market_time import CN_TZ, cn_today

HQ_URL = "https://apphwhq.longhuvip.com/w1/api/index.php"
HIS_URL = "https://apphis.longhuvip.com/w1/api/index.php"
APP_VERSION = "5.23.0.1"
API_VERSION = "w44"
HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; SHARK PRS-A0 Build/PQ3A.190605.01141736)",
    "Accept-Encoding": "gzip",
}


class KaipanlaError(RuntimeError):
    """上游不可达、报错或返回结构不符。"""


def to_symbol(code: str) -> str:
    code = str(code).strip()
    if code.startswith(("60", "68", "90")):
        return f"{code}.SH"
    if code.startswith(("4", "8", "92")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def _num(value) -> float:
    """有限数值; NaN/Inf 与非数字一律视为上游结构异常。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise KaipanlaError(f"数值字段无效: {value!r}") from None
    if not math.isfinite(value):
        raise KaipanlaError(f"数值字段无效: {value!r}")
    return value


def _int(value) -> int:
    return int(_num(value))


def _ratio(value) -> float:
    return round(_num(value) / 100, 6)


def _ts(value) -> str | None:
    value = _num(value)
    if value <= 1e9:
        return None
    return datetime.fromtimestamp(value, CN_TZ).isoformat()


def _plates(value) -> list[str]:
    return [p for p in str(value or "").split("、") if p]


def _limit_up_row(r: list) -> dict:
    return {
        "symbol": to_symbol(r[0]), "name": str(r[1]), "boards": _int(r[15]),
        "first_limit_at": _ts(r[4]), "reason": str(r[5] or ""), "theme_code": str(r[19] or ""),
        "plates": _plates(r[12]), "seal_amount": _num(r[6]), "max_seal": _num(r[7]),
        "reseal": bool(_int(r[16])), "label": str(r[18]) or None,
        "price": _num(r[21]), "change_pct": _ratio(r[22]),
    }


def _broken_row(r: list) -> dict:
    return {
        "symbol": to_symbol(r[0]), "name": str(r[1]), "price": _num(r[4]), "change_pct": _ratio(r[5]),
        "plates": _plates(r[6]), "last_broken_at": _ts(r[13]), "max_seal": _num(r[17]),
    }


_EXPRESSION_KEYS = (
    ("limit_up", _int), ("boards_2", _int), ("boards_3", _int), ("max_boards", _int),
    ("kpl_promo_2", _ratio), ("kpl_promo_3", _ratio), ("kpl_promo_top", _ratio), ("kpl_broken_rate", _ratio),
    ("kpl_yest_limit_perf", _ratio), ("kpl_yest_board_perf", _ratio), ("kpl_yest_broken_perf", _ratio),
)


class KaipanlaClient:
    def __init__(self, device_id: str, *, http: httpx.Client | None = None,
                 min_interval: float = 1.0, timeout: float = 10.0):
        self._common = {"DeviceID": device_id, "PhoneOSNew": "2", "VerSion": APP_VERSION, "apiv": API_VERSION}
        self._http = http or httpx.Client(headers=HEADERS, timeout=timeout)
        self._min_interval = min_interval
        self._lock = Lock()
        self._last = None

    def _call(self, method: str, url: str, params: dict) -> dict:
        data = {**self._common, **{k: str(v) for k, v in params.items()}}
        with self._lock:
            if self._last is not None:
                wait = self._min_interval - (time.monotonic() - self._last)
                if wait > 0:
                    time.sleep(wait)
            try:
                if method == "GET":
                    resp = self._http.get(url, params=data)
                else:
                    resp = self._http.post(url, data=data)
            except httpx.HTTPError as exc:
                raise KaipanlaError(f"请求失败: {exc.__class__.__name__}") from exc
            finally:
                self._last = time.monotonic()
        if resp.status_code != 200:
            raise KaipanlaError(f"HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            raise KaipanlaError("返回不是 JSON") from None
        if not isinstance(body, dict):
            raise KaipanlaError("返回结构不符")
        return body

    def _ok(self, body: dict) -> dict:
        if str(body.get("errcode")) != "0":
            raise KaipanlaError(f"errcode {body.get('errcode')}")
        return body

    def limit_counts(self) -> list[int]:
        info = self._ok(self._call("GET", HQ_URL, {"c": "HomeDingPan", "a": "DailyLimitIndex"})).get("info")
        if not isinstance(info, list) or len(info) != 5:
            raise KaipanlaError("板位统计结构不符")
        return [_int(v) for v in info]

    def limit_up_stocks(self, pid: int) -> tuple[str, list[dict]]:
        body = self._ok(self._call("GET", HQ_URL, {
            "c": "HomeDingPan", "a": "DailyLimitPerformance", "PidType": pid, "Type": 4,
            "Index": 0, "Order": 0, "st": 1000,
        }))
        info = body.get("info")
        if not isinstance(info, list) or len(info) < 2 or not isinstance(info[0], list):
            raise KaipanlaError("涨停列表结构不符")
        try:
            return str(info[1]), [_limit_up_row(r) for r in info[0]]
        except (IndexError, TypeError, KeyError, ValueError, OverflowError, OSError) as exc:
            raise KaipanlaError("涨停行字段缺失") from exc

    def first_board_broken(self) -> list[dict]:
        body = self._ok(self._call("GET", HQ_URL, {
            "c": "HomeDingPan", "a": "DailyLimitPerformance2", "PidType": 1, "Type": 5,
            "Index": 0, "Order": 1, "st": 1000,
        }))
        info = body.get("info")
        if not isinstance(info, list) or not info or not isinstance(info[0], list):
            raise KaipanlaError("破板列表结构不符")
        try:
            return [_broken_row(r) for r in info[0]]
        except (IndexError, TypeError, KeyError, ValueError, OverflowError, OSError) as exc:
            raise KaipanlaError("破板行字段缺失") from exc

    def sentiment(self) -> dict:
        info = self._ok(self._call("GET", HQ_URL, {
            # 参数与实测一致 (Date=当日北京日期, st=1000); 今日地址只返回当日一行
            "c": "HomeDingPan", "a": "ChangeStatistics", "Date": cn_today().isoformat(), "Index": 0, "st": 1000,
        })).get("info")
        if not isinstance(info, list) or not info or not isinstance(info[0], dict):
            raise KaipanlaError("情绪值结构不符")
        row = info[0]
        try:
            return {"day": str(row["Day"]), "kpl_strong": _int(row["strong"]), "limit_up": _int(row["ztjs"]),
                    "max_boards": _int(row["lbgd"]), "sharp_drawdown": _int(row["df_num"])}
        except KeyError as exc:
            raise KaipanlaError(f"情绪值缺字段 {exc}") from None

    def limit_totals(self) -> dict:
        info = self._ok(self._call("POST", HQ_URL, {"c": "HomeDingPan", "a": "MarketStockZDNum"})).get("info")
        if not isinstance(info, dict) or "SJZT" not in info or "SJDT" not in info:
            raise KaipanlaError("涨跌停数结构不符")
        return {"limit_up": _int(info["SJZT"]), "limit_down": _int(info["SJDT"])}

    def expression(self, day: date) -> dict | None:
        body = self._call("GET", HIS_URL, {"c": "HisHomeDingPan", "a": "ZhangTingExpression", "Day": day.isoformat()})
        if str(body.get("errcode")) == "1020":
            return None
        info = self._ok(body).get("info")
        if not isinstance(info, list) or len(info) < 12:
            raise KaipanlaError("涨停表现结构不符")
        out = {key: conv(info[i]) for i, (key, conv) in enumerate(_EXPRESSION_KEYS)}
        out["kpl_summary"] = str(info[11])
        return out
