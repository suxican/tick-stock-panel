"""猫爪数据 / 猫头鹰数据内置数据源 provider。

方法签名对齐 custom.GenericHTTPProvider, 注入 custom loader 注册表后,
各 service 无需改动即可路由到本 provider。

实现数据集 (字段口径以 2026-11 核对的官方 OpenAPI 为准):
  - realtime     股票: `daily` 最新交易日盘中快照 (全市场单请求);
                 ETF 补拉: `etf_daily`; 指数补拉: `indexdaily` 最新快照。
                 未用 `screening` —— 文档登记仅专业版/旗舰版可用。
  - daily        股票 `daily` / ETF `etf_daily` 原始日K。指数日K成交量单位未确认,
                 对 index 空手而归, 由上层回退原日K源。
  - adj_factor   `adj_factor` 为累计复权因子, 相邻交易日比值即单事件 pre/post 比
                 (与 pipeline 的 ex_factor 同向, 除权除息明细示例 dr=1.0046 印证);
                 比值 < 1 说明因子口径与契约不符, 整轮失败不写入。
  - minute       猫头鹰 `minute` / `etf_minute`; 当日走日内服务, 历史日走历史服务。
  - full_minute  修复轮按 24 只/请求取当日全序列; 稳态增量轮按交易分钟取全市场,
                 每根 1 个请求。

单位与口径 (CONTRIBUTING §3, 不凭字段名推断):
  - 日K/快照 vol 文档写明为手, 直接透传; amount 为元。
  - pct_chg 为百分数 (2.03 = 2.03%) → 小数制 /100。
  - 快照 pre_close 为前复权可比昨收 (除权日即交易所参考价), 作涨跌幅基准。
  - 分钟 vol 文档未直接标单位, 但 vwap = 累计额 ÷ (累计量 × 100) 写明累计量以手计,
    逐分钟 vol 为相邻累计量差分, 同为手。
  - 分钟时间: trademin 为收盘桶 (0930 含集合竞价, 0931-1130, 1301-1500 共 241 根),
    datetime = tradedate + trademin 拼北京墙钟 naive (日内 time 为桶内最后一笔时刻,
    不可用), 声明 minute_time_grid=auction_end。
  - 分钟价格: 接口无复权参数, 当日分钟由实时累计行情差分生成 → 当日行声明 raw;
    历史沉淀分钟线官方未写明口径, 不声明 minute_price_basis。
  - 单次最多 6000 行, 达到上限按截断处理 (client 抛 MeozTruncatedError)。
未声明 depth5 (盘口委托量单位未写明) 与 financial, provider_has_dataset 为 False。
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import threading
import time as time_mod
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import polars as pl

from app.data_providers.base import ProviderRequestError
from app.data_providers.normalizer import normalize_daily
from app.market_time import CN_TZ, cn_now
from app.plugins.meoz import client as meoz_client
from app.plugins.meoz.client import MeozAuthError, MeozClient, MeozError, MeozTruncatedError

logger = logging.getLogger(__name__)

_DATASETS = ("realtime", "daily", "adj_factor", "minute", "full_minute")

API_KEY_ENV = "MEOZ_API_KEY"
SECRETS_FIELD = "meoz_api_key"  # UI 配置的 Key 存 secrets.json, 优先级高于 .env

_ROW_BUDGET = 5000  # 单请求计划行数, 低于服务端 6000 行上限
_MAX_SYMBOLS = 800  # 单请求代码数上限, 控制请求体大小
_MAX_WINDOW_DAYS = 2500  # 单请求最多覆盖的交易日数(约 10 年)
_DAILY_DEFAULT_DAYS = 3653
_ADJ_DEFAULT_START = date(2000, 1, 1)
_ADJ_BACKDAYS = 40  # 向前找窗口起点前一交易日的自然日回看
_ADJ_PREFILTER_MIN = 50  # 标的较多时先比对窗口两端因子, 只对有变化的拉序列
_RATIO_EPS = 1e-6
_CALENDAR_SPAN_DAYS = 3000  # 日历按自然日分段查询, 每段远低于行数上限
_CALENDAR_TTL_S = 6 * 3600
_UNIVERSE_TTL_S = 12 * 3600
_UNIVERSE_RETRY_S = 600
# stockbasic 实测 exchange 字段恒为 null, 按交易所过滤返回空 (2026-11);
# 超出行数上限时改按 market 分段, 四段实测覆盖全部上市股票。
_MARKETS = ("主板", "创业板", "科创板", "北交所")
_MINUTE_SYMBOLS_PER_CALL = 24  # 24 × 241 根 = 5784 行 < 6000
_MINUTE_OUTPUT_ROWS = 100_000
_DAILY_CLOSE = time(15, 0)

_QUOTE_FIELDS = "tradedate,symbol,name,open,high,low,close,pre_close,change,pct_chg,vol,amount"
_INDEX_FIELDS = "tradedate,symbol,name,open,high,low,close,pre_close,change,pct_chg,amount,servertime"
_MINUTE_FIELDS = "tradedate,symbol,trademin,time,open,high,low,close,vol,amount"
_ADJ_FIELDS = "tradedate,symbol,code,adj_factor"
_DAILY_API = {"stock": "daily", "etf": "etf_daily"}
_MINUTE_API = {"stock": "minute", "etf": "etf_minute"}

_DAILY_SCHEMA = {
    "symbol": pl.String, "date": pl.Date,
    "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
    "volume": pl.Float64, "amount": pl.Float64,
}
_ADJ_SCHEMA = {"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64}
_MINUTE_SCHEMA = {
    "symbol": pl.String, "datetime": pl.Datetime("us"),
    "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
    "volume": pl.Float64, "amount": pl.Float64,
    "minute_price_basis": pl.String, "minute_time_grid": pl.String,
}


def _session_labels() -> list[str]:
    """1m 收盘桶标签: 0930(含集合竞价) + 0931-1130 + 1301-1500, 共 241 个。"""
    labels = ["0930"]
    for start, end in ((9 * 60 + 31, 11 * 60 + 30), (13 * 60 + 1, 15 * 60)):
        labels.extend(f"{m // 60:02d}{m % 60:02d}" for m in range(start, end + 1))
    return labels


_SESSION_LABELS = _session_labels()


def get_api_key() -> str:
    from app import secrets_store

    return secrets_store.get_env_backed_secret(SECRETS_FIELD, API_KEY_ENV)


def availability() -> tuple[bool, str]:
    """loader 启动自检: API Key 已配置(secrets.json 或 .env)才注册为可切换数据源。不抛异常。"""
    if get_api_key():
        return True, "ok"
    return False, f"缺少 API Key(可在下方输入框直接填写,或配置环境变量 {API_KEY_ENV})"


def probe_api_key(api_key: str) -> tuple[bool, str]:
    """用候选 Key 实探一次交易日历(先探后存, 不落盘)。"""
    client = None
    try:
        client = meoz_client.MeozClient(api_key=api_key, timeout=10.0)
        client.query("tradecal", {})
        return True, "ok"
    except MeozAuthError as e:
        return False, f"Key 无效或无权限: {e}"
    except MeozError as e:
        return False, f"Key 校验失败: {e}"
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()


@dataclass
class _MeozConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "meoz"
    display_name: str = "猫爪数据"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def _to_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # NaN → None


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _parse_ymd(value) -> date | None:
    text = str(value or "").replace("-", "").strip()
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


def _six(symbol: str) -> str:
    return symbol.split(".")[0]


def _beijing_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(CN_TZ).replace(tzinfo=None)
    return value


def _as_date(value: datetime | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _beijing_naive(value).date()
    return value


def _ms(day: date, at: time) -> int:
    return int(datetime.combine(day, at, tzinfo=CN_TZ).timestamp() * 1000)


def _chunks(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _plan(symbols: list[str], days: list[date]) -> list[tuple[list[str], date, date]]:
    """把 标的 × 交易日 切成每请求不超过 _ROW_BUDGET 行的 (代码组, 起, 止)。"""
    plan: list[tuple[list[str], date, date]] = []
    for window in _chunks(days, _MAX_WINDOW_DAYS):
        per_call = max(1, min(_MAX_SYMBOLS, _ROW_BUDGET // len(window)))
        plan.extend((group, window[0], window[-1]) for group in _chunks(symbols, per_call))
    return plan


def _latest_labels(now: datetime, count: int) -> list[str]:
    """截至 now 的最近 count 个收盘桶(含进行中的一根); 集合竞价前为空。"""
    if now.time() < time(9, 25):
        return []
    current = (now.replace(second=0, microsecond=0) + timedelta(minutes=1)).strftime("%H%M")
    current = max(current, "0930")
    upto = [label for label in _SESSION_LABELS if label <= current]
    return upto[-max(1, count):]


def _quote_record(row: dict, symbol: str, today: date, now_ms: int) -> dict:
    """日线最新交易日快照行 → 内部 realtime record。

    tradedate 早于今日(停牌/盘前仍是上一交易日)时戳记为该日收盘, 交给下游按
    quote_ts 日期归属过滤, 不把旧日快照当作今日行情。
    """
    last = _to_float(row.get("close"))
    prev = _to_float(row.get("pre_close"))
    pct = _to_float(row.get("pct_chg"))
    change_pct = pct / 100.0 if pct is not None else None
    change_amount = _to_float(row.get("change"))
    if change_amount is None and last is not None and prev is not None:
        change_amount = last - prev
    if change_pct is None and change_amount is not None and prev not in (None, 0):
        change_pct = change_amount / prev
    trade_day = _parse_ymd(row.get("tradedate"))
    stale = trade_day is not None and trade_day != today
    stamp = _ms(trade_day, _DAILY_CLOSE) if stale else now_ms
    return {
        "symbol": symbol,
        "name": row.get("name"),
        "last_price": last,
        "prev_close": prev,
        "open": _to_float(row.get("open")),
        "high": _to_float(row.get("high")),
        "low": _to_float(row.get("low")),
        "volume": _to_float(row.get("vol")),
        "amount": _to_float(row.get("amount")),
        "change_pct": change_pct,
        "change_amount": change_amount,
        "amplitude": None,
        "turnover_rate": None,  # 需股本口径 (§3.4), 交给 enriched 管道计算
        "timestamp": stamp,
        "session": None,
    }


def _index_record(row: dict, now_ms: int) -> dict:
    """指数快照行 → realtime record。成交量单位官方未确认, 置 None 不透传。"""
    rec = _quote_record(row, str(row.get("symbol") or ""), date.min, now_ms)
    rec["volume"] = None
    trade_day = _parse_ymd(row.get("tradedate"))
    clock = str(row.get("servertime") or "")
    if trade_day is not None:
        try:
            parsed = datetime.strptime(clock, "%H:%M:%S").time()
        except ValueError:
            parsed = _DAILY_CLOSE if trade_day < cn_now().date() else None
        rec["timestamp"] = _ms(trade_day, parsed) if parsed is not None else now_ms
    return rec


def _daily_frame(rows: list[dict], wanted: dict[str, str], drop_day: date | None) -> pl.DataFrame:
    out = []
    for row in rows:
        symbol = row.get("code") if row.get("code") in wanted.values() else wanted.get(
            _six(str(row.get("symbol") or "")))
        day = _parse_ymd(row.get("tradedate"))
        if symbol is None or day is None or day == drop_day:
            continue
        out.append({
            "symbol": symbol, "date": day,
            "open": _to_float(row.get("open")), "high": _to_float(row.get("high")),
            "low": _to_float(row.get("low")), "close": _to_float(row.get("close")),
            "volume": _to_float(row.get("vol")), "amount": _to_float(row.get("amount")),
        })
    return normalize_daily(pl.DataFrame(out, schema=_DAILY_SCHEMA), source="meoz")


def _minute_frame(rows: list[dict], wanted: set[str] | None, day: date,
                  basis: str | None) -> pl.DataFrame:
    out = []
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if not symbol or (wanted is not None and symbol not in wanted):
            continue
        if _parse_ymd(row.get("tradedate")) != day:
            continue
        # trademin 是收盘桶标签; 日内服务的 time 实测为桶内最后一笔时刻
        # (如 09:25:01、14:59:59), 不能作为分钟时间, 仅在缺 trademin 时兜底。
        label = str(row.get("trademin") or "")
        if len(label) == 4 and label.isdigit():
            at = time(int(label[:2]), int(label[2:]))
        else:
            try:
                at = datetime.strptime(str(row.get("time") or ""), "%H:%M:%S").time()
            except ValueError:
                continue
            if at.second:
                continue
        out.append({
            "symbol": symbol, "datetime": datetime.combine(day, at),
            "open": _to_float(row.get("open")), "high": _to_float(row.get("high")),
            "low": _to_float(row.get("low")), "close": _to_float(row.get("close")),
            "volume": _to_float(row.get("vol")), "amount": _to_float(row.get("amount")),
            "minute_price_basis": basis, "minute_time_grid": "auction_end",
        })
    return pl.DataFrame(out, schema=_MINUTE_SCHEMA)


def _concat(parts: list[pl.DataFrame], schema: dict) -> pl.DataFrame:
    parts = [p for p in parts if not p.is_empty()]
    return pl.concat(parts, how="diagonal_relaxed") if parts else pl.DataFrame(schema=schema)


class MeozProvider:
    """猫爪数据源。"""

    name = "meoz"
    builtin = True

    def __init__(self) -> None:
        self.config = _MeozConfig()
        self._client: MeozClient | None = None
        self._lock = threading.Lock()
        self._calendar: dict[tuple[date, date], tuple[float, list[date]]] = {}
        self._universe: dict[str, str] = {}
        self._universe_at = 0.0

    def close(self) -> None:  # loader.load_all 重建注册表时会对每个 provider 调 close
        with self._lock:
            client, self._client = self._client, None
            self._calendar.clear()
            self._universe = {}
            self._universe_at = 0.0
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()

    def _get_client(self) -> MeozClient:
        with self._lock:
            if self._client is None:
                self._client = meoz_client.MeozClient(api_key=get_api_key())
            return self._client

    # ---- 交易日历 / 股票池 ----
    def _trading_days(self, start: date, end: date) -> list[date]:
        if start > end:
            return []
        days: list[date] = []
        cursor = start
        while cursor <= end:
            stop = min(end, cursor + timedelta(days=_CALENDAR_SPAN_DAYS))
            days.extend(self._calendar_span(cursor, stop))
            cursor = stop + timedelta(days=1)
        return sorted(set(days))

    def _calendar_span(self, start: date, end: date) -> list[date]:
        key = (start, end)
        now = time_mod.monotonic()
        with self._lock:
            cached = self._calendar.get(key)
        if cached is not None and (end < cn_now().date() or now - cached[0] < _CALENDAR_TTL_S):
            return cached[1]
        rows = self._get_client().query(
            "tradecal", {"startdate": _ymd(start), "enddate": _ymd(end), "end_latest": False})
        days = []
        for row in rows:
            day = _parse_ymd(row.get("cal_date"))
            if day is None or not (start <= day <= end):
                continue
            if "is_open" in row and str(row.get("is_open")).lower() not in ("1", "true"):
                continue
            days.append(day)
        if rows and not days and not any("cal_date" in r for r in rows):
            raise MeozError("交易日历响应缺少 cal_date 字段, 疑似接口结构变化")
        with self._lock:
            self._calendar[key] = (now, days)
        return days

    def _universe_map(self, *, refresh: bool = False) -> dict[str, str]:
        """6 位股票代码 → 带后缀代码。全量单请求, 达到行数上限时按上市板块分段。"""
        now = time_mod.monotonic()
        with self._lock:
            age = now - self._universe_at
            if self._universe and age < _UNIVERSE_TTL_S and not (refresh and age > _UNIVERSE_RETRY_S):
                return self._universe
        client = self._get_client()
        try:
            rows = client.query("stockbasic", {"list_status": "L"}, "code,symbol")
        except MeozTruncatedError:
            rows = []
            for market in _MARKETS:
                rows.extend(client.query(
                    "stockbasic", {"list_status": "L", "market": market}, "code,symbol"))
        mapping: dict[str, str] = {}
        for row in rows:
            code = str(row.get("code") or "")
            if "." in code:
                mapping[_six(code)] = code
        if not mapping:
            raise MeozError("股票基础信息为空, 无法确定代码交易所后缀")
        with self._lock:
            self._universe, self._universe_at = mapping, now
        return mapping

    # ---- realtime ----
    def _stock_quote_rows(self) -> list[dict]:
        client = self._get_client()
        try:
            return client.query("daily", {}, _QUOTE_FIELDS)
        except MeozTruncatedError:
            sixes = sorted(self._universe_map())
            rows: list[dict] = []
            for group in _chunks(sixes, _MAX_SYMBOLS):
                rows.extend(client.query("daily", {"symbols": ",".join(group)}, _QUOTE_FIELDS))
            return rows

    def get_realtime(self) -> list[dict]:
        """A 股全市场快照 → realtime records。失败软返回空列表(不阻断轮询)。"""
        try:
            rows = self._stock_quote_rows()
            universe = self._universe_map()
            if any(_six(str(r.get("symbol") or "")) not in universe for r in rows):
                universe = self._universe_map(refresh=True)
        except MeozError as e:
            logger.warning("猫爪实时行情拉取失败: %s", e)
            return []
        now_ms = int(time_mod.time() * 1000)
        today = cn_now().date()
        records = []
        unknown = 0
        for row in rows:
            symbol = universe.get(_six(str(row.get("symbol") or "")))
            if symbol is None:
                unknown += 1
                continue
            records.append(_quote_record(row, symbol, today, now_ms))
        if rows and not records:
            logger.warning("猫爪快照 %d 行均无法识别代码, 疑似接口结构变化", len(rows))
            return []
        logger.info("猫爪实时行情拉取完成: %d 条(未识别代码 %d 行)", len(records), unknown)
        return records

    def get_realtime_symbols(self, symbols: list[str], *, asset_type: str = "stock") -> list[dict] | None:
        """补拉指定股票/ETF 的最新快照; 失败返回 None, 成功无数据返回 []。"""
        apiname = _DAILY_API.get(asset_type)
        if apiname is None or not symbols:
            return []
        fields = _QUOTE_FIELDS + (",code" if asset_type == "etf" else "")
        now_ms = int(time_mod.time() * 1000)
        today = cn_now().date()
        records = []
        try:
            for group in _chunks(list(symbols), _MAX_SYMBOLS):
                wanted = {_six(s): s for s in group}
                for row in self._get_client().query(apiname, {"symbols": ",".join(wanted)}, fields):
                    code = row.get("code")
                    symbol = code if code in group else wanted.get(_six(str(row.get("symbol") or "")))
                    if symbol is not None:
                        records.append(_quote_record(row, symbol, today, now_ms))
        except MeozError as e:
            logger.warning("猫爪%s快照补拉失败: %s", asset_type, e)
            return None
        return records

    def get_realtime_indices(self, symbols: list[str]) -> list[dict] | None:
        """指数实时快照; 失败返回 None 保留上轮缓存, 成功无数据返回 []。"""
        wanted = {s for s in symbols if s}
        if not wanted:
            return []
        codes = sorted({_six(s) for s in wanted})
        now_ms = int(time_mod.time() * 1000)
        records = []
        try:
            for group in _chunks(codes, _MAX_SYMBOLS):
                for row in self._get_client().query(
                    "indexdaily", {"symbols": ",".join(group)}, _INDEX_FIELDS,
                ):
                    if row.get("symbol") in wanted:
                        records.append(_index_record(row, now_ms))
        except MeozError as e:
            logger.warning("猫爪指数行情拉取失败: %s", e)
            return None
        return records

    # ---- daily ----
    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        return _concat(list(self.iter_daily(symbols, start_time, end_time, asset_type,
                                            on_chunk_done)), _DAILY_SCHEMA)

    def iter_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> Iterator[pl.DataFrame]:
        """原始日K, 每请求一帧 (≤ 5000 行)。最新交易日盘中行不是终值, 收盘前丢弃。"""
        apiname = _DAILY_API.get(asset_type)
        end_d = _as_date(end_time) or cn_now().date()
        start_d = _as_date(start_time) or end_d - timedelta(days=_DAILY_DEFAULT_DAYS)
        try:
            days = self._trading_days(start_d, end_d) if symbols and apiname else []
        except MeozError as e:
            raise ProviderRequestError(f"猫爪交易日历获取失败: {e}") from e
        plan = _plan(list(symbols), days)
        if not plan:
            if on_chunk_done:
                on_chunk_done(0, 0)
            return
        now = cn_now()
        drop_day = now.date() if now.time() < _DAILY_CLOSE else None
        fields = _QUOTE_FIELDS + (",code" if asset_type == "etf" else "")
        client = self._get_client()
        for i, (group, first, last) in enumerate(plan):
            wanted = {_six(s): s for s in group}
            params = {"symbols": ",".join(wanted), "startdate": _ymd(first), "enddate": _ymd(last)}
            try:
                rows = client.query(apiname, params, fields)
            except MeozError as e:
                raise ProviderRequestError(f"猫爪日K请求失败: {e}") from e
            frame = _daily_frame(rows, wanted, drop_day)
            if on_chunk_done:
                on_chunk_done(i + 1, len(plan))
            if not frame.is_empty():
                yield frame

    # ---- adj_factor ----
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """累计复权因子 → 单事件 ex_factor (相邻交易日比值)。ETF 无因子接口, 空手而归。"""
        empty = pl.DataFrame(schema=_ADJ_SCHEMA)
        end_d = _as_date(end_time) or cn_now().date()
        start_d = _as_date(start_time) or _ADJ_DEFAULT_START
        if not symbols or asset_type != "stock" or start_d > end_d:
            if on_chunk_done:
                on_chunk_done(0, 0)
            return empty
        try:
            days = self._trading_days(start_d - timedelta(days=_ADJ_BACKDAYS), end_d)
            window = [d for d in days if d >= start_d]
            before = [d for d in days if d < start_d]
            if not window:
                if on_chunk_done:
                    on_chunk_done(0, 0)
                return empty
            ref = before[-1] if before else None
            progress = _Progress(on_chunk_done)
            targets = list(symbols)
            if ref is not None and len(targets) > _ADJ_PREFILTER_MIN:
                progress.add(2 * len(_chunks(targets, _MAX_SYMBOLS)))
                head = self._factors_on(ref, targets, progress)
                tail = self._factors_on(window[-1], targets, progress)
                targets = [s for s in targets if s not in head or head[s] != tail.get(s)]
            series_days = ([ref] if ref is not None else []) + window
            plan = _plan(targets, series_days)
            progress.add(len(plan))
            series: dict[str, list[tuple[date, float]]] = {}
            for group, first, last in plan:
                for symbol, day, factor in self._factor_rows(
                    group, {"startdate": _ymd(first), "enddate": _ymd(last)}):
                    series.setdefault(symbol, []).append((day, factor))
                progress.step()
            progress.finish()
        except MeozError as e:
            raise ProviderRequestError(f"猫爪复权因子请求失败: {e}") from e
        return _derive_ex_factors(series, start_d)

    def _factor_rows(self, group: list[str], params: dict) -> list[tuple[str, date, float]]:
        wanted = {_six(s): s for s in group}
        out = []
        rows = self._get_client().query(
            "adj_factor", {"symbols": ",".join(wanted), **params}, _ADJ_FIELDS)
        for row in rows:
            code = row.get("code")
            symbol = code if code in group else wanted.get(_six(str(row.get("symbol") or "")))
            day = _parse_ymd(row.get("tradedate"))
            factor = _to_float(row.get("adj_factor"))
            if symbol is not None and day is not None and factor is not None and factor > 0:
                out.append((symbol, day, factor))
        return out

    def _factors_on(self, day: date, symbols: list[str], progress: _Progress) -> dict[str, float]:
        out: dict[str, float] = {}
        for group in _chunks(symbols, _MAX_SYMBOLS):
            for symbol, _, factor in self._factor_rows(group, {"tradedate": _ymd(day)}):
                out[symbol] = factor
            progress.step()
        return out

    # ---- minute ----
    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        return _concat(list(self.iter_minute(
            symbols, start_time, end_time, asset_type=asset_type, freq=freq,
            on_chunk_done=on_chunk_done,
        )), _MINUTE_SCHEMA)

    def iter_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> Iterator[pl.DataFrame]:
        """1 分钟K: 按交易日 × 24 只请求全日序列, 再按窗口过滤。指数分钟成交量单位未确认, 不提供。"""
        apiname = _MINUTE_API.get(asset_type)
        now = cn_now().replace(tzinfo=None)
        end = _beijing_naive(end_time) or now
        start = _beijing_naive(start_time) or datetime.combine(end.date(), time(0, 0))
        try:
            days = (self._trading_days(start.date(), end.date())
                    if symbols and apiname and freq == "1m" and start <= end else [])
        except MeozError as e:
            raise ProviderRequestError(f"猫爪交易日历获取失败: {e}") from e
        groups = _chunks(list(symbols), _MINUTE_SYMBOLS_PER_CALL)
        total = len(days) * len(groups)
        if not total:
            if on_chunk_done:
                on_chunk_done(0, 0)
            return
        client = self._get_client()
        parts: list[pl.DataFrame] = []
        pending = 0
        done = 0
        for day in days:
            service = "live" if day == now.date() else "history"
            for group in groups:
                params = {"symbols": ",".join(_six(s) for s in group), "tradedate": _ymd(day)}
                try:
                    rows = client.query(apiname, params, _MINUTE_FIELDS, service=service)
                except MeozError as e:
                    raise ProviderRequestError(f"猫爪分钟K请求失败: {e}") from e
                frame = _minute_frame(rows, set(group), day, "raw" if service == "live" else None)
                frame = frame.filter(pl.col("datetime").is_between(start, end))
                if not frame.is_empty():
                    parts.append(frame)
                    pending += frame.height
                if pending >= _MINUTE_OUTPUT_ROWS:
                    yield pl.concat(parts, how="diagonal_relaxed")
                    parts, pending = [], 0
                done += 1
                if on_chunk_done:
                    on_chunk_done(done, total)
        if parts:
            yield pl.concat(parts, how="diagonal_relaxed")

    def get_intraday_batch(
        self, symbols: list[str], count: int = 300, asset_type: str = "stock",
    ) -> pl.DataFrame:
        """全量分钟修复轮: 当日全序列, 24 只/请求; 单组失败跳过, 凭据被拒则中止本轮。"""
        apiname = _MINUTE_API.get(asset_type)
        if not symbols or apiname is None:
            return pl.DataFrame(schema=_MINUTE_SCHEMA)
        today = cn_now().date()
        client = self._get_client()
        parts: list[pl.DataFrame] = []
        failures = 0
        first_error = ""
        groups = _chunks(list(symbols), _MINUTE_SYMBOLS_PER_CALL)
        for group in groups:
            params = {"symbols": ",".join(_six(s) for s in group), "tradedate": _ymd(today)}
            try:
                rows = client.query(apiname, params, _MINUTE_FIELDS)
            except MeozAuthError as e:
                logger.warning("猫爪全量分钟修复轮中止: %s", e)
                break
            except MeozError as e:
                failures += 1
                first_error = first_error or str(e)
                continue
            parts.append(_minute_frame(rows, set(group), today, "raw"))
        if failures:
            logger.warning("猫爪全量分钟修复轮: %d/%d 组失败 (首个错误: %s)",
                           failures, len(groups), first_error)
        return _concat(parts, _MINUTE_SCHEMA)

    def get_intraday_latest(self, symbols: list[str] | None = None, count: int = 3) -> pl.DataFrame:
        """全量分钟稳态增量轮: 每个交易分钟 1 个全市场请求, 共 count 个。"""
        now = cn_now().replace(tzinfo=None)
        labels = _latest_labels(now, max(1, min(int(count), 30)))
        if not labels:
            return pl.DataFrame(schema=_MINUTE_SCHEMA)
        wanted = set(symbols) if symbols else None
        today = now.date()
        client = self._get_client()
        parts = []
        for label in labels:
            params = {"tradedate": _ymd(today), "trademin": label}
            try:
                rows = client.query("minute", params, _MINUTE_FIELDS)
            except MeozTruncatedError:
                rows = []
                for group in _chunks(sorted(self._universe_map()), _ROW_BUDGET // 2):
                    rows.extend(client.query(
                        "minute", {**params, "symbols": ",".join(group)}, _MINUTE_FIELDS))
            parts.append(_minute_frame(rows, wanted, today, "raw"))
        return _concat(parts, _MINUTE_SCHEMA)

    # ---- 设置页试拉 ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        syms = [s for s in (symbols or [])][:3] or ["600519.SH", "000001.SZ"]
        now = datetime.now(CN_TZ)
        try:
            if dataset == "realtime":
                records = self.get_realtime_symbols(syms, asset_type="stock")
                if records is None:
                    raise MeozError("快照请求失败, 详见后端日志")
                frame = pl.DataFrame(records) if records else pl.DataFrame()
            elif dataset == "daily":
                frame = self.get_daily(syms, now - timedelta(days=30), now)
            elif dataset == "adj_factor":
                frame = self.get_adj_factors(syms, now - timedelta(days=365), now)
            elif dataset == "minute":
                frame = self.get_minute(syms, now - timedelta(days=3), now)
            elif dataset == "full_minute":
                frame = self.get_intraday_batch(syms)
            else:
                return {"provider": self.name, "dataset": dataset, "rows": 0,
                        "error": f"猫爪插件未接入 {dataset} 数据集(按能力路由回退)"}
        except (MeozError, ProviderRequestError) as e:
            return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
        head = frame.head(5).to_dicts()
        for row in head:  # date/datetime → ISO 字符串, 保证 JSON 可序列化
            for k, v in list(row.items()):
                if isinstance(v, (date, datetime)):
                    row[k] = v.isoformat()
        return {"provider": self.name, "dataset": dataset, "rows": frame.height,
                "columns": frame.columns, "preview": head}


class _Progress:
    """两阶段请求的进度回报: 总量随阶段规划增加, 结束时 cur == total。"""

    def __init__(self, callback: Callable[[int, int], None] | None) -> None:
        self._callback = callback
        self.done = 0
        self.total = 0

    def add(self, planned: int) -> None:
        self.total += planned

    def step(self) -> None:
        self.done += 1
        if self._callback:
            self._callback(self.done, self.total)

    def finish(self) -> None:
        if self._callback and self.done == 0:
            self._callback(0, 0)


def _derive_ex_factors(series: dict[str, list[tuple[date, float]]], start: date) -> pl.DataFrame:
    """相邻交易日因子比值 → 单事件 ex_factor; 比值 < 1 与契约方向相反, 整轮失败。"""
    out = []
    for symbol, points in series.items():
        points = sorted(set(points))
        for (_, prev), (day, cur) in itertools.pairwise(points):
            ratio = cur / prev
            if abs(ratio - 1.0) <= _RATIO_EPS or day < start:
                continue
            if ratio < 1.0:
                raise ProviderRequestError(
                    f"猫爪复权因子在 {symbol} {day.isoformat()} 下降 (比值 {ratio:.6f}), "
                    "与单事件 pre/post 比值契约不符, 本轮不写入")
            out.append({"symbol": symbol, "trade_date": day, "ex_factor": ratio})
    return pl.DataFrame(out, schema=_ADJ_SCHEMA).sort(["symbol", "trade_date"])


__all__ = [
    "API_KEY_ENV",
    "MeozProvider",
    "availability",
    "get_api_key",
    "probe_api_key",
]
