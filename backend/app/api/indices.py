"""指数页固定目录、行情详情与境内指数同步。"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Annotated

import polars as pl
from fastapi import APIRouter, HTTPException, Query, Request

from app.indicators.pipeline import compute_enriched
from app.market_time import cn_today
from app.services import (  # noqa: F401
    index_sync,
    kline_sync,
    overseas_indices,
    preferences,
    ths_indices,
    trading_day,
)
from app.services.index_const import INDEX_PAGE_BY_SYMBOL, OVERSEAS_INDEX_SYMBOLS, THS_ALL_A_SYMBOL
from app.tickflow.capabilities import Cap

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/index", tags=["index"])

# 指数分钟结果进程内缓存: 吸收板块切换卡片/指数页轮询与重挂载的重复请求
_INDEX_MINUTE_CACHE_TTL = 10.0
_INDEX_MINUTE_CACHE_MAX = 32
_index_minute_cache: dict[tuple[str, str, str], tuple[float, pl.DataFrame]] = {}
_index_minute_cache_lock = threading.Lock()


@router.get("/catalog")
def get_index_catalog():
    return {"rows": list(INDEX_PAGE_BY_SYMBOL.values())}


@router.get("/overseas-quotes")
def get_overseas_quotes():
    return overseas_indices.get_quotes()


@router.get("/ths-quotes")
def get_ths_quotes():
    return ths_indices.get_quotes()


def _index_info(repo, symbol: str) -> dict:
    df = repo.get_index_instruments()
    if df.is_empty() or "symbol" not in df.columns:
        return {}
    hit = df.filter(pl.col("symbol") == symbol).head(1)
    if hit.is_empty():
        return {}
    return hit.to_dicts()[0]



@router.get("/daily")
def get_index_daily(
    request: Request,
    symbol: str = Query(..., description="指数代码, 如 000001.SH"),
    days: int = Query(120, ge=10, le=2000),
    start_date: str | None = Query(None, description="起始日期 YYYY-MM-DD, 优先于 days"),
    end_date: str | None = Query(None, description="截止日期 YYYY-MM-DD, 默认今天"),
):
    """读取指数日 K。指数数据使用独立 kline_index_* parquet。"""
    # 未传 end_date 时用北京今天。实时注入只在内存缓存命中时补当日 K,
    # 缓存冷时 parquet 当日行能否进结果取决于这个窗口右端。
    try:
        end = date.fromisoformat(end_date) if end_date else cn_today()
        start = date.fromisoformat(start_date) if start_date else end - timedelta(days=days)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(status_code=422, detail="日期格式应为 YYYY-MM-DD") from exc
    if start > end:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
    if symbol == THS_ALL_A_SYMBOL:
        if (end - start).days > 5 * 366:
            raise HTTPException(status_code=422, detail="同花顺指数单次查询最多覆盖 5 年")
        info = INDEX_PAGE_BY_SYMBOL[symbol]
        return {"symbol": symbol, "name": info["name"], "index_info": info,
                **ths_indices.get_daily(symbol, start, end)}
    if symbol in OVERSEAS_INDEX_SYMBOLS:
        if (end - start).days > 5000:
            raise HTTPException(status_code=422, detail="海外指数单次查询最多覆盖 5000 天")
        info = INDEX_PAGE_BY_SYMBOL[symbol]
        return {"symbol": symbol, "name": info["name"], "index_info": info,
                **overseas_indices.get_daily(symbol, start, end)}
    repo = request.app.state.repo
    info = _index_info(repo, symbol)

    df = repo.get_index_daily(symbol, start, end)
    if not df.is_empty():
        from app.api.kline import _maybe_inject_live_candle
        rows = _maybe_inject_live_candle(request, symbol, df.to_dicts(), "index")
        return {"symbol": symbol, "name": info.get("name"), "index_info": info, "rows": rows, "source": "index_enriched"}

    capset = request.app.state.capabilities
    if not capset.has(Cap.KLINE_DAILY_BATCH):
        return {"symbol": symbol, "name": info.get("name"), "index_info": info, "rows": [], "source": "none"}

    try:
        raw = kline_sync.sync_daily_batch([symbol], count=days + 150)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TickFlow fetch failed: {e}") from e
    if raw.is_empty():
        return {"symbol": symbol, "name": info.get("name"), "index_info": info, "rows": [], "source": "none"}

    enriched = compute_enriched(raw, factors=None, instruments=None)
    rows = enriched.filter((pl.col("date") >= start) & (pl.col("date") <= end)).to_dicts()
    return {"symbol": symbol, "name": info.get("name"), "index_info": info, "rows": rows, "source": "live"}


@router.get("/minute")
def get_index_minute(
    request: Request,
    symbol: str = Query(..., description="指数代码, 如 000001.SH"),
    trade_date: Annotated[date | None, Query(alias="date", description="交易日期, 休市时默认最近本地指数交易日")] = None,
):
    """实时读取指数分钟 K。不写入股票分钟 parquet。

    历史深度由当前分钟源决定, 显式日期不会因空数据而替换。
    未指定日期且确认休市时使用最近本地指数日 K 的日期。
    结果带 10s 进程内缓存, 不同指数、日期和分钟源分别缓存。
    """
    if symbol in OVERSEAS_INDEX_SYMBOLS or symbol == THS_ALL_A_SYMBOL:
        info = INDEX_PAGE_BY_SYMBOL[symbol]
        return {
            "symbol": symbol, "name": info["name"], "index_info": info,
            "date": trade_date, "rows": [], "source": "unsupported",
        }
    repo = request.app.state.repo
    capset = request.app.state.capabilities
    info = _index_info(repo, symbol)
    today = cn_today()
    day = trade_date or today
    if trade_date is None and trading_day.is_trading_day() is False:
        daily = repo.get_index_daily(symbol, today - timedelta(days=366), today, columns=["date"])
        if not daily.is_empty():
            day = daily["date"].max() or today
    if day > today:
        return {
            "symbol": symbol,
            "name": info.get("name"),
            "index_info": info,
            "date": str(day),
            "rows": [],
            "source": "future",
        }
    cache_key = (symbol, day.isoformat(), kline_sync._selected_minute_provider_name())
    now = time.monotonic()
    with _index_minute_cache_lock:
        hit = _index_minute_cache.get(cache_key)
    if hit is not None and now - hit[0] < _INDEX_MINUTE_CACHE_TTL:
        df = hit[1]
    else:
        df = kline_sync.fetch_minute_single(symbol, day, asset_type="index", capset=capset)
        with _index_minute_cache_lock:
            _index_minute_cache[cache_key] = (time.monotonic(), df)
            while len(_index_minute_cache) > _INDEX_MINUTE_CACHE_MAX:
                oldest = min(_index_minute_cache, key=lambda k: _index_minute_cache[k][0])
                del _index_minute_cache[oldest]
    return {
        "symbol": symbol,
        "name": info.get("name"),
        "index_info": info,
        "date": str(day),
        "rows": df.to_dicts(),
        "source": "live" if not df.is_empty() else "none",
    }


@router.post("/sync_instruments")
def sync_index_instruments(request: Request):
    """同步 CN_Index 指数标的列表。"""
    repo = request.app.state.repo
    count = index_sync.sync_index_instruments(repo)
    return {"status": "ok", "count": count}


@router.post("/sync_daily")
def sync_index_daily(
    request: Request,
    days: int = Query(365, ge=30, le=5000),
):
    """同步指数日K到独立 parquet。"""
    repo = request.app.state.repo
    capset = request.app.state.capabilities
    if not capset.has(Cap.KLINE_DAILY_BATCH):
        raise HTTPException(status_code=403, detail="需要 Pro+ 权限 (batch K-line)")
    end = datetime.now()
    start = end - timedelta(days=days)
    count = index_sync.sync_index_instruments(repo)
    rows = index_sync.sync_and_persist_index_daily(repo, capset, start_date=start, end_date=end)
    return {"status": "ok", "index_count": count, "rows_written": rows}
