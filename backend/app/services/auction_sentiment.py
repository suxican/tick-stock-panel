"""情绪周期新维度: 竞价情绪 (集合竞价 09:25 定版) — 只读本地归档。

数据:
- 集合竞价按日归档 (market_reference/v1/auction, 猫爪 daily_auc 2019-01 起): 竞价价、涨幅、成交额。
- 交易所按日涨跌停价 (market_reference/v1/price_limits, 2020 起): 判定竞价涨停/跌停与无涨跌幅新股。
- 日线 enriched 的 consecutive_limit_ups >= 1 = 收盘封住, 与封板质量 / 情绪周期涨停数同口径。

口径 (ST 按情绪统计开关剔除, 按日名单优先; 无涨跌幅限制的新股一律剔除):
- 全市场: high_open_ratio / low_open_ratio = 竞价涨幅 >0 / <0 的家数占有竞价成交家数;
  median_pct 竞价涨幅中位数; limit_up_open / limit_down_open 竞价价等于当日交易所涨/跌停价的
  家数 (无涨跌停价的日子为 null); amount 竞价成交额, amount_ratio_5d 相对前 5 个交易日均值。
- 赚钱效应: 上一交易日收盘封住的股 (prev_sealed, 只计今天有竞价成交的) 今天的竞价涨幅均值
  prev_mean_pct、高开占比 prev_high_open_ratio、竞价 <= -3% 的家数 prev_weak_count。
- 只对同时有竞价分区和日线分区的交易日出行 (缺任一不按 0 处理)。
- 不做竞价封单额推算: 实测 daily_auc 的 um_vol 远小于涨停池封单 (盛剑科技 23 手 vs 4340 万元),
  不是买方涨停排队量。
不改 regime 时序与阶段规则 (规则改动须先离线验证)。
"""

from __future__ import annotations

import logging
import threading
from datetime import date
from pathlib import Path

import polars as pl

from app.services.seal_quality import (
    PCT_MIN_SAMPLES,
    PCT_WINDOW,
    _enriched_days,
    _exclude_st_default,
    _signature,
)

logger = logging.getLogger(__name__)

WEAK_PCT = -0.03                # 昨日封住股竞价 <= -3% 记为「核按钮」
NEW_STOCK_PCT = 0.205           # 无交易所涨跌停价的日子 (2020 前), 竞价涨幅超出此值视为无涨跌幅新股
LIMIT_EPS = 0.005               # 竞价价与涨跌停价的比较容差 (元)
AMOUNT_WINDOW = 5
METRICS = ("high_open_ratio", "median_pct", "limit_up_open", "limit_down_open", "prev_mean_pct",
           "prev_high_open_ratio", "prev_weak_count", "amount_ratio_5d")

_ENRICHED = "kline_daily_enriched"
_AUCTION = ("market_reference", "v1", "auction")
_LIMITS = ("market_reference", "v1", "price_limits")

_lock = threading.Lock()
_cache: dict[tuple, pl.DataFrame] = {}


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _current_st_symbols(data_dir: Path) -> frozenset[str]:
    from app.services.market_mainline import load_risk_warning_symbols

    return load_risk_warning_symbols(data_dir)


def _partition_days(folder: Path) -> list[date]:
    days = []
    for path in folder.glob("date=*.parquet") if folder.is_dir() else ():
        try:
            days.append(date.fromisoformat(path.stem.removeprefix("date=")))
        except ValueError:
            continue
    return sorted(days)


def _empty() -> pl.DataFrame:
    return pl.DataFrame(schema={
        "date": pl.Date, "count": pl.Int64, "high_open_ratio": pl.Float64, "low_open_ratio": pl.Float64,
        "median_pct": pl.Float64, "limit_up_open": pl.Int64, "limit_down_open": pl.Int64,
        "prev_sealed": pl.Int64, "prev_mean_pct": pl.Float64, "prev_high_open_ratio": pl.Float64,
        "prev_weak_count": pl.Int64, "amount": pl.Float64, "amount_ratio_5d": pl.Float64,
    })


def _exclude_st(frame: pl.DataFrame, data_dir: Path, exclude_st: bool) -> pl.DataFrame:
    if not exclude_st or frame.is_empty():
        return frame
    from app.services.market_reference import exclude_risk_warning

    return exclude_risk_warning(frame, _current_st_symbols(data_dir))


def compute_auction_sentiment(data_dir: Path, *, exclude_st: bool | None = None) -> pl.DataFrame:
    """全量计算每日竞价情绪, 按日期升序。"""
    data_dir = Path(data_dir)
    enriched_all = sorted(_enriched_days(data_dir / _ENRICHED))
    enriched_set = set(enriched_all)
    days = [d for d in _partition_days(data_dir.joinpath(*_AUCTION)) if d in enriched_set]
    if not days:
        return _empty()
    if exclude_st is None:
        exclude_st = _exclude_st_default()
    auction = (pl.scan_parquet(str(data_dir.joinpath(*_AUCTION) / "date=*.parquet"))
               .filter(pl.col("date").is_in(days)).select("date", "symbol", "price", "change_pct", "amount")
               .collect())
    limit_days = set(_partition_days(data_dir.joinpath(*_LIMITS)))
    if limit_days & set(days):
        limits = (pl.scan_parquet(str(data_dir.joinpath(*_LIMITS) / "date=*.parquet"))
                  .filter(pl.col("date").is_in(days)).select("date", "symbol", "limit_up", "limit_down", "no_limit")
                  .collect())
    else:
        limits = pl.DataFrame(schema={"date": pl.Date, "symbol": pl.String, "limit_up": pl.Float64,
                                      "limit_down": pl.Float64, "no_limit": pl.Boolean})
    rows = auction.join(limits, on=["date", "symbol"], how="left").with_columns(
        pl.col("date").is_in(sorted(limit_days)).alias("_has_limits"))
    # 新股上市初期无涨跌幅限制: 有交易所涨跌停价的日子按 no_limit, 之前按竞价涨幅超出常规限幅判断
    new_stock = (pl.when(pl.col("_has_limits")).then(pl.col("no_limit").fill_null(False))
                 .otherwise(pl.col("change_pct").abs() > NEW_STOCK_PCT))
    rows = _exclude_st(rows.filter(~new_stock), data_dir, exclude_st)

    up_hit = (pl.col("price") - pl.col("limit_up")).abs() < LIMIT_EPS
    down_hit = (pl.col("price") - pl.col("limit_down")).abs() < LIMIT_EPS
    market = rows.group_by("date").agg(
        pl.len().cast(pl.Int64).alias("count"),
        (pl.col("change_pct") > 0).mean().alias("high_open_ratio"),
        (pl.col("change_pct") < 0).mean().alias("low_open_ratio"),
        pl.col("change_pct").median().alias("median_pct"),
        pl.when(pl.col("_has_limits").any()).then(up_hit.fill_null(False).sum()).otherwise(None)
        .cast(pl.Int64).alias("limit_up_open"),
        pl.when(pl.col("_has_limits").any()).then(down_hit.fill_null(False).sum()).otherwise(None)
        .cast(pl.Int64).alias("limit_down_open"),
        pl.col("amount").sum().alias("amount"),
    )

    # 赚钱效应: 上一交易日收盘封住的股今天的竞价表现
    previous = {d: enriched_all[i - 1] for i, d in enumerate(enriched_all) if i > 0}
    prev_days = sorted({previous[d] for d in days if d in previous})
    if prev_days:
        sealed = (pl.scan_parquet(str(data_dir / _ENRICHED / "*" / "part.parquet"))
                  .select("symbol", pl.col("date").cast(pl.Date), "consecutive_limit_ups")
                  .filter((pl.col("consecutive_limit_ups") >= 1) & pl.col("date").is_in(prev_days))
                  .select("symbol", "date").collect())
        sealed = _exclude_st(sealed, data_dir, exclude_st)
        mapping = pl.DataFrame({"date": [d for d in days if d in previous],
                                "_prev": [previous[d] for d in days if d in previous]},
                               schema={"date": pl.Date, "_prev": pl.Date})
        cohort = (sealed.rename({"date": "_prev"}).join(mapping, on="_prev")
                  .join(rows.select("date", "symbol", "change_pct"), on=["date", "symbol"]))
        prev = cohort.group_by("date").agg(
            pl.len().cast(pl.Int64).alias("prev_sealed"),
            pl.col("change_pct").mean().alias("prev_mean_pct"),
            (pl.col("change_pct") > 0).mean().alias("prev_high_open_ratio"),
            (pl.col("change_pct") <= WEAK_PCT).sum().cast(pl.Int64).alias("prev_weak_count"),
        )
    else:
        prev = pl.DataFrame(schema={"date": pl.Date, "prev_sealed": pl.Int64, "prev_mean_pct": pl.Float64,
                                    "prev_high_open_ratio": pl.Float64, "prev_weak_count": pl.Int64})

    frame = (pl.DataFrame({"date": days}, schema={"date": pl.Date})
             .join(market, on="date", how="left").join(prev, on="date", how="left")
             .with_columns(pl.col("count").fill_null(0), pl.col("prev_sealed").fill_null(0),
                           pl.col("prev_weak_count").fill_null(0))
             .sort("date"))
    # 前 5 个交易日竞价成交额均值 (不含当天); 不足 5 天为 null
    base = pl.col("amount").shift(1).rolling_mean(AMOUNT_WINDOW, min_samples=AMOUNT_WINDOW)
    frame = frame.with_columns(pl.when(base > 0).then(pl.col("amount") / base).otherwise(None).alias("amount_ratio_5d"))
    return frame.select(_empty().columns).cast(_empty().schema)


def load_auction_sentiment(data_dir: Path) -> pl.DataFrame:
    """带进程内缓存的全量结果 (签名: 竞价目录 + 涨跌停价目录 + 日线目录, ST 开关)。"""
    data_dir = Path(data_dir)
    exclude_st = _exclude_st_default()
    key = (str(data_dir), exclude_st, _signature(data_dir.joinpath(*_AUCTION)),
           _signature(data_dir.joinpath(*_LIMITS)), _signature(data_dir / _ENRICHED))
    with _lock:
        hit = _cache.get(key)
    if hit is not None:
        return hit
    frame = compute_auction_sentiment(data_dir, exclude_st=exclude_st)
    with _lock:
        _cache.clear()
        _cache[key] = frame
    return frame


def _percentiles(frame: pl.DataFrame) -> dict[str, float | None]:
    window = frame.tail(PCT_WINDOW)
    latest = window.row(-1, named=True)
    out: dict[str, float | None] = {}
    for metric in METRICS:
        values = window[metric].drop_nulls()
        value = latest[metric]
        if value is None or values.len() < PCT_MIN_SAMPLES:
            out[metric] = None
        else:
            out[metric] = round(float((values <= value).sum()) / values.len() * 100, 1)
    return out


def _jsonable(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if isinstance(v, date):
            out[k] = v.isoformat()
        elif isinstance(v, float):
            out[k] = round(v, 0) if k == "amount" else round(v, 4)
        else:
            out[k] = v
    return out


def auction_sentiment_payload(data_dir: Path, start: date | None, end: date | None) -> dict:
    """接口返回: 区间内每日行 + 最新一日 (近一年分位基于全量, 不受区间影响)。"""
    frame = load_auction_sentiment(data_dir)
    base = {"rows": [], "latest": None, "first_date": None}
    if frame.is_empty():
        return base
    ranged = frame
    if start is not None:
        ranged = ranged.filter(pl.col("date") >= start)
    if end is not None:
        ranged = ranged.filter(pl.col("date") <= end)
    upto = frame.filter(pl.col("date") <= end) if end is not None else frame
    latest = None
    if not upto.is_empty():
        latest = {**_jsonable(upto.row(-1, named=True)), "pct": _percentiles(upto)}
    return {**base, "rows": [_jsonable(r) for r in ranged.iter_rows(named=True)], "latest": latest,
            "first_date": frame["date"][0].isoformat()}
