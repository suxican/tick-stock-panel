"""Overseas index quotes and bars from Tencent and Sina public quote endpoints.

Both endpoints serve their own web pages and are reachable from mainland China,
where Yahoo Finance is not. They are not guaranteed developer APIs. Requests use
no credentials or cookies and are never retried or switched to another source.
Sina rejects requests without its own site as Referer; nothing else is spoofed.
"""
from __future__ import annotations

import math
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from app import __version__

_SOURCE = "cn_overseas"
_TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
_TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_SINA_QUOTE_URL = "https://hq.sinajs.cn/list="
_SINA_DAILY_URL = "https://gi.finance.sina.com.cn/hq/daily"
_SINA_HEADERS = {"Referer": "https://finance.sina.com.cn/"}
_TIMEOUT = 8.0
# Vendors round the change percentage to two decimals.
_PCT_TOLERANCE = 0.006
_TENCENT_MAX_BARS = 2000


@dataclass(frozen=True)
class _Instrument:
    symbol: str
    name: str
    market: str
    currency: str
    timezone: str
    vendor: str          # "tencent" | "sina"
    quote_code: str      # code in the batched quote request
    identity: str        # vendor field proving the instrument (code or name)
    daily_code: str
    session: tuple[time, time]


_US = (time(9, 30), time(16, 0))
_ASIA = (time(9, 0), time(15, 30))

_INDICES = {spec.symbol: spec for spec in (
    _Instrument("^IXIC", "纳斯达克综合指数", "US", "USD", "America/New_York",
                "tencent", "usIXIC", ".IXIC", "us.IXIC", _US),
    _Instrument("^DJI", "道琼斯工业平均指数", "US", "USD", "America/New_York",
                "tencent", "usDJI", ".DJI", "us.DJI", _US),
    _Instrument("^GSPC", "标普500", "US", "USD", "America/New_York",
                "tencent", "usINX", ".INX", "us.INX", _US),
    _Instrument("^N225", "日经225", "JP", "JPY", "Asia/Tokyo",
                "sina", "znb_NKY", "日经225", "NKY", _ASIA),
    _Instrument("^KS11", "韩国 KOSPI", "KR", "KRW", "Asia/Seoul",
                "sina", "znb_KOSPI", "首尔综合指数", "KOSPI", _ASIA),
)}

# Market-game order is part of the contract; Korean equities have no domestic source.
_GAME_SYMBOLS = (
    ("^IXIC", None), ("^KS11", None),
    ("005930.KS", ("三星电子", "KR", "KRW")), ("000660.KS", ("SK 海力士", "KR", "KRW")),
)
_NO_EQUITY = "国内行情源暂不覆盖韩国个股, 暂不可用"

_TENCENT_LINE = re.compile(r'v_(\w+)="([^"]*)"')
_SINA_LINE = re.compile(r'var hq_str_(\w+)="([^"]*)"')


@dataclass
class _Config:
    name: str = _SOURCE
    display_name: str = "腾讯/新浪 海外行情"
    datasets: dict = field(default_factory=lambda: {"overseas": None})
    path: None = None
    builtin: bool = True


def availability() -> tuple[bool, str]:
    """Dependency check only; registration must never access the network."""
    return True, "ok"


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("海外行情采集时间必须包含时区")
    return value.astimezone(UTC)


def _number(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("报价数值无法核验") from exc
    if not math.isfinite(number):
        raise ValueError("报价数值无法核验")
    return number


def _positive(value) -> float:
    number = _number(value)
    if number <= 0:
        raise ValueError("报价数值无法核验")
    return number


def _source_url(spec: _Instrument) -> str:
    if spec.vendor == "tencent":
        return f"https://gu.qq.com/{spec.quote_code}"
    return f"https://finance.sina.com.cn/stock/globalindex/quotes/{spec.daily_code}"


def _empty(symbol: str, name: str, market: str, currency: str, reason: str,
           state: str = "unavailable", source_url: str | None = None) -> dict:
    return {
        "symbol": symbol, "name": name, "market": market, "currency": currency,
        "state": state, "price": None, "previous_close": None, "change_pct": None,
        "session_date": None, "observed_at": None, "available_at": None, "phase": "unknown",
        "source_url": source_url, "reason": reason,
    }


def _spec_empty(spec: _Instrument, reason: str, state: str = "unavailable") -> dict:
    return _empty(spec.symbol, spec.name, spec.market, spec.currency, reason, state, _source_url(spec))


def _session_end(spec: _Instrument, day: date) -> datetime:
    return datetime.combine(day, spec.session[1], ZoneInfo(spec.timezone))


def _phase(spec: _Instrument, observed: datetime, fetched: datetime) -> str:
    tz = ZoneInfo(spec.timezone)
    day = observed.astimezone(tz).date()
    start = datetime.combine(day, spec.session[0], tz)
    end = _session_end(spec, day)
    if start <= observed < end and fetched < end:
        return "intraday"
    # Vendors stamp the last update, which may come after the closing print.
    if fetched >= end and observed >= end - timedelta(seconds=60):
        return "closed"
    return "unknown"


def _ready(spec: _Instrument, price: float, previous: float, vendor_pct: float,
           observed: datetime, fetched: datetime) -> dict:
    if observed > fetched:
        raise ValueError("报价时间晚于实际采集时间")
    change = price / previous - 1
    if abs(change * 100 - vendor_pct) > _PCT_TOLERANCE:
        raise ValueError("报价与前收不一致, 暂不计算涨跌幅")
    row = _spec_empty(spec, "按数据源前收计算; 行情可能延迟, 延迟时长未经确认", "ready")
    row.update(
        price=price, previous_close=previous, change_pct=change,
        session_date=observed.astimezone(ZoneInfo(spec.timezone)).date().isoformat(),
        observed_at=observed.astimezone(UTC).isoformat(), available_at=fetched.isoformat(),
        phase=_phase(spec, observed, fetched),
    )
    return row


def _parse_tencent(spec: _Instrument, fields: list[str], fetched: datetime) -> dict:
    # Indices: 2 code, 3 price, 4 previous close, 30 New York time, 32 change %, 35 currency.
    if len(fields) < 36 or fields[2] != spec.identity or fields[35] != spec.currency:
        raise ValueError("标的或币种不一致")
    observed = datetime.strptime(fields[30], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo(spec.timezone))
    return _ready(spec, _positive(fields[3]), _positive(fields[4]), _number(fields[32]), observed, fetched)


def _parse_sina(spec: _Instrument, fields: list[str], fetched: datetime) -> dict:
    # 0 name, 1 price, 2 change, 3 change %, 6/7 Beijing date and time, 9 previous close.
    if len(fields) < 12 or fields[0] != spec.identity:
        raise ValueError("标的名称不一致")
    price, previous = _positive(fields[1]), _positive(fields[9])
    if abs(price - previous - _number(fields[2])) > max(0.011, price * 1e-6):
        raise ValueError("报价与前收不一致, 暂不计算涨跌幅")
    observed = datetime.strptime(f"{fields[6]} {fields[7]}", "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=ZoneInfo("Asia/Shanghai"))
    return _ready(spec, price, previous, _number(fields[3]), observed, fetched)


def _local_bar_is_final(spec: _Instrument, day: date, fetched: datetime) -> bool:
    today = fetched.astimezone(ZoneInfo(spec.timezone)).date()
    return day < today or (day == today and fetched >= _session_end(spec, day))


class CnOverseasProvider:
    name = _SOURCE
    builtin = True

    def __init__(self, *, clock: Callable[[], datetime] = _now,
                 client_factory: Callable[[], httpx.Client] | None = None) -> None:
        self.config = _Config()
        self._clock = clock
        self._client_factory = client_factory or (
            lambda: httpx.Client(timeout=_TIMEOUT, follow_redirects=False, headers={
                "User-Agent": f"TickStockPanel/{__version__}",
            })
        )

    def validate(self) -> list[str]:
        return []

    def close(self) -> None:
        # Each explicit acquisition owns and closes its client before returning.
        pass

    def _vendor_quotes(self, client: httpx.Client, vendor: str, specs: list[_Instrument]) -> dict:
        try:
            codes = ",".join(spec.quote_code for spec in specs)
            if vendor == "tencent":
                response = client.get(_TENCENT_QUOTE_URL + codes, timeout=_TIMEOUT)
                pattern, encoding = _TENCENT_LINE, "gbk"
            else:
                response = client.get(_SINA_QUOTE_URL + codes, headers=_SINA_HEADERS, timeout=_TIMEOUT)
                pattern, encoding = _SINA_LINE, "gb18030"
            if response.status_code != 200:
                return {spec.symbol: _spec_empty(spec, "数据源请求未成功, 暂不可用") for spec in specs}
            lines = dict(pattern.findall(response.content.decode(encoding, errors="replace")))
        except httpx.HTTPError:
            return {spec.symbol: _spec_empty(spec, "数据源连接失败或超时, 暂不可用") for spec in specs}
        fetched = _utc(self._clock())
        parse = _parse_tencent if vendor == "tencent" else _parse_sina
        rows = {}
        for spec in specs:
            body = lines.get(spec.quote_code, "")
            fields = body.split("~" if vendor == "tencent" else ",")
            if not body or (vendor == "tencent" and len(fields) < 3):
                rows[spec.symbol] = _spec_empty(spec, "数据源未返回可用行情")
                continue
            try:
                rows[spec.symbol] = parse(spec, fields, fetched)
            except ValueError as exc:
                rows[spec.symbol] = _spec_empty(spec, str(exc), "unverified")
        return rows

    def _fetch_quotes(self, symbols) -> dict:
        specs = [_INDICES[symbol] for symbol in symbols]
        groups = {vendor: [spec for spec in specs if spec.vendor == vendor] for vendor in ("tencent", "sina")}
        groups = {vendor: items for vendor, items in groups.items() if items}
        rows: dict = {}
        with self._client_factory() as client, ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="overseas",
        ) as executor:
            for result in executor.map(lambda item: self._vendor_quotes(client, *item), groups.items()):
                rows.update(result)
        return rows

    def get_overseas_quotes(self) -> dict:
        _utc(self._clock())
        rows = self._fetch_quotes([symbol for symbol, equity in _GAME_SYMBOLS if equity is None])
        fetched_at = _utc(self._clock()).isoformat()
        quotes = []
        for symbol, equity in _GAME_SYMBOLS:
            if equity is not None:
                quotes.append(_empty(symbol, *equity, _NO_EQUITY))
                continue
            row = rows[symbol]
            if row["available_at"] is not None:
                row["available_at"] = fetched_at
            quotes.append(row)
        return {"source": _SOURCE, "fetched_at": fetched_at, "quotes": quotes}

    def get_overseas_index_quotes(self) -> list[dict]:
        return list(self._fetch_quotes(list(_INDICES)).values())

    def get_overseas_index_daily(self, symbol: str, start: date, end: date) -> list[dict]:
        spec = _INDICES.get(symbol)
        if spec is None:
            raise ValueError("不支持此海外指数")
        with self._client_factory() as client:
            if spec.vendor == "tencent":
                count = min(_TENCENT_MAX_BARS, (end - start).days + 1)
                response = client.get(_TENCENT_KLINE_URL, timeout=_TIMEOUT, params={
                    "param": f"{spec.daily_code},day,{start.isoformat()},{end.isoformat()},{count},qfq",
                })
                response.raise_for_status()
                data = response.json()["data"][spec.daily_code]
                raw = data.get("day") or data.get("qfqday") or []
                # Row: date, open, close, high, low, volume.
                bars = [(r[0], r[1], r[3], r[4], r[2], r[5] if len(r) > 5 else None) for r in raw]
            else:
                # The endpoint returns the latest ~1000 sessions regardless of range.
                response = client.get(_SINA_DAILY_URL, params={"symbol": spec.daily_code},
                                      headers=_SINA_HEADERS, timeout=_TIMEOUT)
                response.raise_for_status()
                raw = response.json()["result"]["data"] or []
                bars = [(r["d"], r["o"], r["h"], r["l"], r["c"], r.get("v")) for r in raw]
        fetched = _utc(self._clock())
        rows = []
        for day_text, *ohlc_text, volume_text in bars:
            day = date.fromisoformat(day_text)
            if not start <= day <= end or not _local_bar_is_final(spec, day, fetched):
                continue
            if rows and day.isoformat() <= rows[-1]["date"]:
                raise ValueError("日线日期重复或错序")
            ohlc = dict(zip(("open", "high", "low", "close"), map(_positive, ohlc_text), strict=True))
            volume = _number(volume_text) if volume_text not in (None, "") else None
            rows.append({"symbol": symbol, "date": day.isoformat(), **ohlc,
                         "volume": volume if volume is not None and volume > 0 else None})
        return rows

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset != "overseas":
            return {"provider": self.name, "dataset": dataset, "rows": 0,
                    "columns": [], "preview": [], "error": "此插件仅提供海外行情"}
        rows = self.get_overseas_index_quotes()
        valid_count = sum(row["state"] == "ready" for row in rows)
        return {"provider": self.name, "dataset": dataset, "rows": valid_count,
                "columns": list(rows[0]), "preview": rows,
                **({"error": "海外行情暂不可用, 请查看各标的原因"} if not valid_count else {})}
