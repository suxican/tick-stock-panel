"""Normalize public overseas quotes and index bars independently of A shares.

The chart endpoint is also used by the yfinance maintainer's history scraper.
It is not a guaranteed Yahoo developer API. Requests never use credentials,
cookies, browser impersonation, alternate hosts, or automatic retries.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from app import __version__

_SOURCE = "yahoo_overseas"
_BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"
_PARAMS = {
    "interval": "1d", "range": "5d", "includePrePost": "false", "events": "div,splits",
}


@dataclass(frozen=True)
class _Instrument:
    symbol: str
    name: str
    market: str
    currency: str
    timezone: str
    kind: str


_INSTRUMENTS = (
    _Instrument("^IXIC", "纳斯达克综合指数", "US", "USD", "America/New_York", "INDEX"),
    _Instrument("^KS11", "韩国 KOSPI", "KR", "KRW", "Asia/Seoul", "INDEX"),
    _Instrument("005930.KS", "三星电子", "KR", "KRW", "Asia/Seoul", "EQUITY"),
    _Instrument("000660.KS", "SK 海力士", "KR", "KRW", "Asia/Seoul", "EQUITY"),
)

_PAGE_INDICES = {spec.symbol: spec for spec in (
    *_INSTRUMENTS[:2],
    _Instrument("^DJI", "道琼斯工业平均指数", "US", "USD", "America/New_York", "INDEX"),
    _Instrument("^GSPC", "标普500", "US", "USD", "America/New_York", "INDEX"),
    _Instrument("^N225", "日经225", "JP", "JPY", "Asia/Tokyo", "INDEX"),
)}


@dataclass
class _Config:
    name: str = _SOURCE
    display_name: str = "Yahoo Finance 海外行情"
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


def _price(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("报价字段不是有效数值")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError("报价数值超出范围") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError("报价字段不是有效正数")
    return number


def _timestamp(value) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("报价时间缺失")
    try:
        if not math.isfinite(value) or value <= 0 or int(value) != value:
            raise ValueError("报价时间无效")
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("报价时间无效") from exc


def _empty(instrument: _Instrument, reason: str, state: str = "unavailable") -> dict:
    return {
        "symbol": instrument.symbol, "name": instrument.name, "market": instrument.market,
        "currency": instrument.currency, "state": state, "price": None,
        "previous_close": None, "change_pct": None, "session_date": None,
        "observed_at": None, "available_at": None, "phase": "unknown",
        "source_url": f"https://finance.yahoo.com/quote/{quote(instrument.symbol, safe='')}/",
        "reason": reason,
    }


def _phase(meta: dict, observed: datetime, fetched: datetime, tz: ZoneInfo) -> str:
    """A regular-period descriptor may already refer to the next session."""
    try:
        period = meta["currentTradingPeriod"]["regular"]
        start, end = _timestamp(period["start"]), _timestamp(period["end"])
        if not start < end or start.astimezone(tz).date() != observed.astimezone(tz).date():
            return "unknown"
        if end.astimezone(tz).date() != observed.astimezone(tz).date():
            return "unknown"
        if start <= observed < end and fetched < end:
            return "intraday"
        if fetched >= end and end - timedelta(seconds=60) <= observed <= end + timedelta(seconds=60):
            return "closed"
    except (KeyError, TypeError, ValueError):
        pass
    return "unknown"


def _comparison(result: dict, observed: datetime, instrument: _Instrument, *, quote_only: bool) -> float:
    """Use adjacent returned sessions, never the range's chartPreviousClose."""
    tz = ZoneInfo(instrument.timezone)
    session_date = observed.astimezone(tz).date()
    timestamps = result.get("timestamp")
    if not isinstance(timestamps, list) or not 2 <= len(timestamps) <= 20:
        raise ValueError("前一交易日收盘数据不足")
    instants = [_timestamp(value) for value in timestamps]
    dates = [value.astimezone(tz).date() for value in instants]
    if dates != sorted(set(dates)) or any(value > observed for value in instants):
        raise ValueError("日线日期重复、错序或晚于报价时间")
    if dates[-1] != session_date:
        raise ValueError("最新报价与日线所属交易日不一致")
    indicators = result.get("indicators")
    if not isinstance(indicators, dict):
        raise ValueError("日线字段缺失")
    quotes = indicators.get("quote")
    if not isinstance(quotes, list) or len(quotes) != 1 or not isinstance(quotes[0], dict):
        raise ValueError("日线字段缺失")
    prices = quotes[0]
    for column in ("open", "high", "low", "close"):
        if not isinstance(prices.get(column), list) or len(prices[column]) != len(dates):
            raise ValueError("日线字段长度不一致")
    # Do not skip an invalid preceding bar and silently use an older close.
    latest = _price(result["meta"].get("regularMarketPrice"))
    for index in (-2, -1):
        values = {key: prices[key][index] for key in ("open", "high", "low", "close")}
        # The index quote screen can use a regular-session print even before the
        # final close is published. This does not create a completed daily bar.
        if index == -1 and quote_only and values["close"] is None:
            values["close"] = latest
        values = {key: _price(value) for key, value in values.items()}
        if not values["low"] <= min(values["open"], values["close"]) <= max(values["open"], values["close"]) <= values["high"]:
            raise ValueError("相邻交易日价格范围无法核验")
    # A quote and bars can disagree because of adjustment/scale changes or
    # incomplete updates. Do not turn that disagreement into a market return.
    hint = result["meta"].get("priceHint")
    if hint is not None and (type(hint) is not int or not 0 <= hint <= 8):
        raise ValueError("报价精度无法核验")
    rounding = .5 * 10 ** -hint if hint is not None else 0
    tolerance = max(rounding, values["high"] * 1e-10)
    if not values["low"] - tolerance <= latest <= values["high"] + tolerance:
        raise ValueError("最新报价与当日日线价格范围不一致, 暂不计算涨跌幅")
    if instrument.kind == "EQUITY":
        events = result.get("events", {})
        if not isinstance(events, dict):
            raise ValueError("除权事件字段无法核验")
        for event_type in ("dividends", "splits", "capitalGains"):
            items = events.get(event_type, {})
            if not isinstance(items, dict):
                raise ValueError("除权事件字段无法核验")
            for item in items.values():
                if not isinstance(item, dict):
                    raise ValueError("除权事件字段无法核验")
                event_date = _timestamp(item.get("date")).astimezone(tz).date()
                if dates[-2] < event_date <= session_date:
                    raise ValueError("比较窗口跨除权事件, 暂不计算涨跌幅")
        adjusted = indicators.get("adjclose")
        if not isinstance(adjusted, list) or len(adjusted) != 1 or not isinstance(adjusted[0], dict):
            raise ValueError("缺少股票除权口径校验数据")
        values = adjusted[0].get("adjclose")
        if not isinstance(values, list) or len(values) != len(dates):
            raise ValueError("缺少股票除权口径校验数据")
        ratios = [_price(values[index]) / _price(prices["close"][index]) for index in (-2, -1)]
        if (not all(math.isfinite(value) for value in ratios)
                or not math.isclose(ratios[0], ratios[1], rel_tol=1e-5, abs_tol=1e-8)):
            raise ValueError("相邻交易日除权口径变化, 暂不计算涨跌幅")
    return _price(prices["close"][-2])


def _matches(meta: dict, spec: _Instrument) -> bool:
    return (meta.get("symbol") == spec.symbol and meta.get("instrumentType") == spec.kind
            and meta.get("currency") == spec.currency and meta.get("exchangeTimezoneName") == spec.timezone)


def _normalize(payload: dict, instrument: _Instrument, fetched: datetime, *, quote_only: bool = False) -> dict:
    row = _empty(instrument, "返回数据无法核验", "unverified")
    try:
        chart = payload["chart"]
        results = chart["result"]
        if chart.get("error") or not isinstance(results, list) or len(results) != 1:
            return _empty(instrument, "数据源未返回可用行情")
        result = results[0]
        meta = result["meta"]
        if not _matches(meta, instrument):
            row["reason"] = "标的、资产类别、币种或交易时区不一致"
            return row
        observed = _timestamp(meta.get("regularMarketTime"))
        if observed > fetched:
            row["reason"] = "报价时间晚于实际采集时间"
            return row
        price = _price(meta.get("regularMarketPrice"))
        tz = ZoneInfo(instrument.timezone)
        row.update(
            price=price, session_date=observed.astimezone(tz).date().isoformat(),
            observed_at=observed.isoformat(), available_at=fetched.isoformat(),
            phase=_phase(meta, observed, fetched, tz),
        )
        previous = _comparison(result, observed, instrument, quote_only=quote_only)
        change = price / previous - 1
        if not math.isfinite(change):
            raise ValueError("涨跌幅无法计算")
        row.update(
            previous_close=previous, change_pct=change,
            state="ready", reason="按相邻交易日收盘计算; 行情可能延迟, 延迟时长未经确认",
        )
    except ValueError as exc:
        row["reason"] = str(exc)
    except (KeyError, TypeError, AttributeError):
        row["reason"] = "数据源返回字段缺失或格式变化"
    return row


class YahooOverseasProvider:
    name = _SOURCE
    builtin = True

    def __init__(self, *, clock: Callable[[], datetime] = _now,
                 client_factory: Callable[[], httpx.Client] | None = None) -> None:
        self.config = _Config()
        self._clock = clock
        self._client_factory = client_factory or (
            lambda: httpx.Client(timeout=8.0, follow_redirects=False, headers={
                "User-Agent": f"TickStockPanel/{__version__}", "Accept": "application/json",
            })
        )

    def validate(self) -> list[str]:
        return []

    def close(self) -> None:
        # Each explicit acquisition owns and closes its client before returning.
        pass

    def _fetch_quotes(self, specs, *, quote_only: bool = False) -> list[dict]:
        with self._client_factory() as client, ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="overseas",
        ) as executor:
            return list(executor.map(lambda spec: self._fetch(client, spec, quote_only=quote_only), specs))

    def get_overseas_quotes(self) -> dict:
        _utc(self._clock())
        rows = self._fetch_quotes(_INSTRUMENTS)
        fetched_at = _utc(self._clock()).isoformat()
        for row in rows:
            if row["available_at"] is not None:
                row["available_at"] = fetched_at
        return {"source": _SOURCE, "fetched_at": fetched_at, "quotes": rows}

    def get_overseas_index_quotes(self) -> list[dict]:
        return self._fetch_quotes(_PAGE_INDICES.values(), quote_only=True)

    def get_overseas_index_daily(self, symbol: str, start: date, end: date) -> list[dict]:
        spec = _PAGE_INDICES.get(symbol)
        if spec is None:
            raise ValueError("不支持此海外指数")
        tz = ZoneInfo(spec.timezone)
        params = {
            "interval": "1d", "includePrePost": "false",
            "period1": int(datetime.combine(start, time.min, tz).timestamp()),
            "period2": int(datetime.combine(end + timedelta(days=1), time.min, tz).timestamp()),
        }
        with self._client_factory() as client:
            response = client.get(_BASE_URL + quote(symbol, safe=""), params=params, timeout=8.0)
            response.raise_for_status()
            chart = response.json()["chart"]
        if chart.get("error") or len(chart["result"]) != 1:
            raise ValueError("数据源未返回指数日线")
        result = chart["result"][0]
        if not _matches(result["meta"], spec):
            raise ValueError("日线标的、币种或交易时区不一致")
        timestamps = result.get("timestamp", [])
        if not timestamps:
            return []
        bars = result["indicators"]["quote"][0]
        keys = ("open", "high", "low", "close")
        if any(len(bars[key]) != len(timestamps) for key in keys):
            raise ValueError("日线字段长度不一致")
        volume = bars.get("volume")
        if volume is not None and len(volume) != len(timestamps):
            raise ValueError("成交量字段长度不一致")
        fetched = _utc(self._clock())
        rows = []
        for i, ts in enumerate(timestamps):
            instant = _timestamp(ts)
            day = instant.astimezone(tz).date()
            if instant > fetched or not start <= day <= end or any(bars[key][i] is None for key in keys):
                continue
            ohlc = {key: _price(bars[key][i]) for key in keys}
            rows.append({"symbol": symbol, "date": day.isoformat(), **ohlc,
                         "volume": volume[i] if volume is not None else None})
        return rows

    def _fetch(self, client: httpx.Client, instrument: _Instrument, *, quote_only: bool = False) -> dict:
        try:
            response = client.get(
                _BASE_URL + quote(instrument.symbol, safe=""), params=_PARAMS, timeout=8.0,
            )
            if response.status_code == 429:
                return _empty(instrument, "数据源限流, 暂不可用")
            if response.status_code != 200:
                return _empty(instrument, "数据源请求未成功, 暂不可用")
            payload = response.json()
            if not isinstance(payload, dict):
                return _empty(instrument, "数据源返回格式无法核验", "unverified")
            return _normalize(payload, instrument, _utc(self._clock()), quote_only=quote_only)
        except httpx.HTTPError:
            return _empty(instrument, "数据源连接失败或超时, 暂不可用")
        except (ValueError, TypeError):
            return _empty(instrument, "数据源返回格式无法核验", "unverified")

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset != "overseas":
            return {"provider": self.name, "dataset": dataset, "rows": 0,
                    "columns": [], "preview": [], "error": "此插件仅提供海外行情"}
        result = self.get_overseas_quotes()
        rows = result["quotes"]
        valid_count = sum(row["state"] == "ready" for row in rows)
        return {"provider": self.name, "dataset": dataset, "rows": valid_count,
                "columns": list(rows[0]), "preview": rows,
                **({"error": "海外行情暂不可用, 请查看各标的原因"} if not valid_count else {})}
