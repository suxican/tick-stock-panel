"""自在量化 REST 适配器。价格仅使用原始日 K，累计 factor 不进入除权事件库。"""
from __future__ import annotations

import math
import os
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from threading import Lock

import httpx
import polars as pl

from app.market_time import CN_TZ, cn_now

_DAY_PATH = "/v3/market/kline/day"
_PAGE_SIZE = 6000
_WINDOW_DAYS = 365  # 避开单股 1000 条上限及服务端长窗口截断。
_INTERVAL = 2.0  # 本插件保守节奏，非服务端配额承诺。
_SYMBOL = re.compile(r"[0-9]{6}\.(?:SZ|SH|BJ)")


class ZzshareError(RuntimeError):
    """采集失败；上层不得将其等同于成功无记录。"""


@dataclass
class _Config:
    name: str = "zzshare"
    display_name: str = "zzshare 自在量化"
    datasets: dict = field(default_factory=lambda: {"daily": None})
    path: None = None
    builtin: bool = True


def _number(row: dict, key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ZzshareError(f"日 K 缺少有效数值 {key}")
    return float(value)


def _daily_frame(rows: list, symbols: set[str], start: date, end: date, *, market: bool) -> pl.DataFrame:
    records = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not _SYMBOL.fullmatch(str(row.get("ts_code"))):
            raise ZzshareError("日 K 股票代码缺失或非法")
        symbol = row["ts_code"]
        if market and symbol not in symbols:
            continue
        if symbol not in symbols:
            raise ZzshareError("日 K 返回其他股票")
        try:
            day = date.fromisoformat(row["trade_date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ZzshareError("日 K 日期无效") from exc
        if not start <= day <= end or (symbol, day) in seen:
            raise ZzshareError("日 K 日期越界或记录重复")
        seen.add((symbol, day))
        if row.get("is_paused") in (True, "1"):
            continue
        volume, amount = _number(row, "volume"), _number(row, "turnover")
        if volume == amount == 0:
            continue
        prices = {key: _number(row, key) for key in ("open", "high", "low", "close")}
        low, high = prices["low"], prices["high"]
        if (low <= 0 or not low <= prices["open"] <= high or not low <= prices["close"] <= high
                or volume <= 0 or amount < 0):
            raise ZzshareError("日 K 价格或量额无效")
        records.append({"symbol": symbol, "date": day, **prices,
                        "volume": math.floor(volume / 100), "amount": amount})
    return pl.DataFrame(records).sort("symbol", "date") if records else pl.DataFrame()


class ZzshareProvider:
    name = "zzshare"
    builtin = True

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self.config = _Config()
        self._http = httpx.Client(base_url="https://api.zizizaizai.com", timeout=20, transport=transport)
        self._lock = Lock()
        self._next_request = 0.0

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, params: dict):
        with self._lock:
            delay = self._next_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._next_request = time.monotonic() + _INTERVAL
        try:
            response = self._http.get(path, params=params, headers={
                "sdk-key": os.environ.get("ZZSHARE_TOKEN", "").strip() or "anonymous",
            })
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            raise ZzshareError(f"zzshare HTTP {exc.response.status_code}") from exc
        except (httpx.RequestError, ValueError) as exc:
            raise ZzshareError("zzshare 网络错误或响应不是 JSON") from exc
        if not isinstance(body, dict) or body.get("code") not in (200, 20000):
            raise ZzshareError("zzshare 返回业务错误")
        return body.get("data")

    def get_limit_reasons(self, day: str) -> list[dict]:
        """按 ISO 日期取原始归因记录；同股多条记录交上层归并。"""
        target = date.fromisoformat(day)
        rows = self._get("/v3/open/review/uplimit/reason", {"date1": target.strftime("%Y%m%d")})
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or row.get("date") != target.isoformat()
            or not all(isinstance(row.get(k), str) for k in ("stock_code", "reason"))
            for row in rows
        ):
            raise ZzshareError("涨停原因日期或记录格式不符")
        return rows

    def _bars(self, path: str, **params) -> list:
        data = self._get(path, {**params, "candle_mode": 0})
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise ZzshareError("日 K 缺少记录列表")
        if data.get("candle_mode", 0) != 0:
            raise ZzshareError("上游返回了复权日 K")
        return data["list"]

    def _market_day(self, symbols: set[str], day: date) -> Iterator[pl.DataFrame]:
        offset = 0
        seen = set()
        while True:
            rows = self._bars(_DAY_PATH, trade_date=day.strftime("%Y%m%d"), limit=_PAGE_SIZE, offset=offset)
            if not rows:
                return
            # 防范 offset 被上游忽略导致重复计入 / 无限循环，包含未选中的标的。
            codes = {r.get("ts_code") for r in rows if isinstance(r, dict)}
            if len(codes) != len(rows) or seen.intersection(codes):
                raise ZzshareError("全市场日 K 分页包含重复股票")
            seen.update(codes)
            yield _daily_frame(rows, symbols, day, day, market=True)
            # count 是本页行数。服务端若降低页大小，不能把短页误当作已扫全市场。
            if symbols.issubset(seen):
                return
            offset += len(rows)

    def iter_daily(self, symbols: list[str], start_time: datetime | None, end_time: datetime | None,
                   asset_type: str = "stock", on_chunk_done: Callable[[int, int], None] | None = None
                   ) -> Iterator[pl.DataFrame]:
        if asset_type != "stock" or not symbols:
            return
        pool = set(symbols)
        if any(not _SYMBOL.fullmatch(s) for s in pool):
            raise ZzshareError("日 K 需要带交易所后缀的股票代码")
        end_dt = end_time or cn_now()
        start_dt = start_time or end_dt - timedelta(days=365)
        start, end = [(dt.astimezone(CN_TZ) if dt.tzinfo else dt).date() for dt in (start_dt, end_dt)]
        days = (end - start).days + 1
        if days <= 0:
            return
        windows = math.ceil(days / _WINDOW_DAYS)
        if len(pool) >= 32 and days <= len(pool) * windows:
            for i in range(days):
                day = start + timedelta(days=i)
                yield from self._market_day(pool, day)
                if on_chunk_done:
                    on_chunk_done(i + 1, days)
            return
        completed = 0
        for symbol in sorted(pool):
            for i in range(windows):
                first = start + timedelta(days=i * _WINDOW_DAYS)
                last = min(end, first + timedelta(days=_WINDOW_DAYS - 1))
                rows = self._bars(f"{_DAY_PATH}/{symbol}", get_type="range",
                                  start_date=first.strftime("%Y%m%d"), end_date=last.strftime("%Y%m%d"),
                                  limit=1000, offset=0)
                yield _daily_frame(rows, {symbol}, first, last, market=False)
                completed += 1
                if on_chunk_done:
                    on_chunk_done(completed, windows * len(pool))

    def get_daily(self, symbols, start_time, end_time, asset_type="stock", on_chunk_done=None) -> pl.DataFrame:
        frames = [f for f in self.iter_daily(symbols, start_time, end_time, asset_type, on_chunk_done)
                  if not f.is_empty()]
        return pl.concat(frames) if frames else pl.DataFrame()

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        result = {"provider": self.name, "dataset": dataset, "rows": 0}
        if dataset != "daily":
            return {**result, "error": f"zzshare 未接入 {dataset}"}
        end = cn_now()
        try:
            frame = self.get_daily((symbols or ["600000.SH"])[:3], end - timedelta(days=14), end)
        except ZzshareError as exc:
            return {**result, "error": str(exc)}
        preview = frame.head(5).to_dicts()
        for row in preview:
            row["date"] = row["date"].isoformat()
        return {**result, "rows": frame.height, "columns": frame.columns, "preview": preview}
