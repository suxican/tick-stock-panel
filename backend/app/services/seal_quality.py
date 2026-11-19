"""情绪周期新维度: 封板质量 (炸板率 / 开板率 / 首封时间分布 / 封单额) — 只读本地归档。

数据:
- 猫爪涨停池按日归档 (market_reference/v1/limit_events, 2018-09-12 起): 首封时间、开板次数、封单额。
  `type` 是收盘集合竞价前的状态, 尾盘回封会被标成炸板 (ub)。
- 日线 enriched 的 consecutive_limit_ups >= 1 = 收盘封住, 与现有情绪周期涨停数同口径, 以它为准。

口径 (只看涨停一侧, ST 按情绪统计开关剔除, 按日名单优先):
- touched = 涨停池 u/ub ∪ 收盘封住; sealed = 收盘封住; break_rate = 1 - sealed / touched
- reopen_rate / early_share / late_share / oneword_share / fd_median 只统计「收盘封住且在涨停池
  有记录」的股, pool_coverage 给出该覆盖率。open_times 在 2020-01-02 前为字段缺失 (0 或 null),
  该区间 reopen_rate 记 null, 不记 0; 此后个别行为 null (实测全部是 ST 股) 时按首封 != 末封判断。
- 只对同时有涨停池分区和日线分区的交易日出行 (缺任一不按 0 处理)。

全量计算约 1-2.3 秒 (实测 1983 天, 含按日 ST 判定), 不另存汇总表; 进程内缓存, 签名为涨停池目录
与参与计算的日线日期目录 (盘中当日日线反复重写不会让缓存失效)。
不改 regime 时序与阶段规则 (规则改动须先离线验证)。
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import date
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

REOPEN_FROM = date(2020, 1, 2)   # open_times 自此日起才有真实值
EARLY_CUTOFF = "10:00:00"        # 早盘封板: 首封 <= 10:00
LATE_FROM = "14:30:00"           # 尾盘封板: 首封 >= 14:30
ONEWORD_CUTOFF = "09:30:00"      # 开盘即封 (含竞价一字)
FD_RATIO_MIN = 0.005             # 封单额/成交额日中位低于此值视为口径异常
PCT_WINDOW = 250                 # 近一年分位窗口 (交易日)
PCT_MIN_SAMPLES = 60
METRICS = ("break_rate", "reopen_rate", "early_share", "late_share", "oneword_share", "fd_median")

_ENRICHED = "kline_daily_enriched"
_POOL = ("market_reference", "v1", "limit_events")

_lock = threading.Lock()
_cache: dict[tuple, pl.DataFrame] = {}


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _current_st_symbols(data_dir: Path) -> frozenset[str]:
    from app.services.market_mainline import load_risk_warning_symbols

    return load_risk_warning_symbols(data_dir)


def _exclude_st_default() -> bool:
    try:
        from app.services import preferences

        return bool(preferences.get_sentiment_exclude_st())
    except Exception:  # noqa: BLE001 — 偏好读取失败按默认剔除
        return True


def _signature(folder: Path) -> tuple[int, int]:
    count, latest = 0, 0
    if not folder.is_dir():
        return 0, 0
    for entry in os.scandir(folder):
        count += 1
        try:
            latest = max(latest, entry.stat().st_mtime_ns)
        except OSError:
            continue
    return count, latest


def _pool_days(pool_dir: Path) -> list[date]:
    days = []
    for path in pool_dir.glob("date=*.parquet") if pool_dir.is_dir() else ():
        try:
            days.append(date.fromisoformat(path.stem.removeprefix("date=")))
        except ValueError:
            continue
    return sorted(days)


def _enriched_days(enriched_dir: Path) -> set[date]:
    days: set[date] = set()
    for entry in os.scandir(enriched_dir) if enriched_dir.is_dir() else ():
        if entry.name.startswith("date="):
            try:
                days.add(date.fromisoformat(entry.name.removeprefix("date=")))
            except ValueError:
                continue
    return days


def _days(data_dir: Path) -> list[date]:
    """同时有涨停池分区和日线分区的交易日。

    只有涨停池没有日线 (管道延后/失败) 时不出行: 否则 sealed=0 会给出 100% 炸板的误导值。
    """
    enriched = _enriched_days(data_dir / _ENRICHED)
    return [d for d in _pool_days(data_dir.joinpath(*_POOL)) if d in enriched]


def _enriched_signature(data_dir: Path, days: list[date]) -> tuple[int, int]:
    """只看参与计算的日线日期目录: 盘中当日 enriched 每 ~15 秒原子重写, 当日涨停池落盘前
    这些写入不影响结果, 不应让缓存失效。依赖写入方用 temp + os.replace (目录 mtime 随之更新)。"""
    latest = 0
    base = data_dir / _ENRICHED
    for d in days:
        try:
            latest = max(latest, (base / f"date={d.isoformat()}").stat().st_mtime_ns)
        except OSError:
            continue
    return len(days), latest


def _empty() -> pl.DataFrame:
    return pl.DataFrame(schema={
        "date": pl.Date, "touched": pl.Int64, "sealed": pl.Int64, "break_rate": pl.Float64,
        "reopen_rate": pl.Float64, "early_share": pl.Float64, "late_share": pl.Float64,
        "oneword_share": pl.Float64, "fd_median": pl.Float64, "pool_coverage": pl.Float64,
    })


def compute_seal_quality(data_dir: Path, *, exclude_st: bool | None = None) -> pl.DataFrame:
    """全量计算每日封板质量, 按日期升序。"""
    data_dir = Path(data_dir)
    pool_dir = data_dir.joinpath(*_POOL)
    days = _days(data_dir)
    if not days:
        return _empty()
    if exclude_st is None:
        exclude_st = _exclude_st_default()
    pool = (
        pl.scan_parquet(str(pool_dir / "date=*.parquet"))
        .filter(pl.col("type").is_in(["u", "ub"]) & pl.col("date").is_in(days))
        .select("date", "symbol", "name", "first_time", "last_time", "open_times", "fd_amount", "amount")
        .with_columns(pl.lit(True).alias("_in_pool"))
        .collect()
    )
    # 按涨停池「当时的股票名」剔除: 新股上市初期 (N/C 开头, 涨跌幅规则不同, 日线不按涨停计);
    # ST (2021 年前没有按日名单, 当前名单会漏掉已摘帽的历史 ST)。同一股同日的日线行一并剔除。
    name = pl.col("name").fill_null("")
    drop = name.str.starts_with("N") | name.str.starts_with("C")
    if exclude_st:
        drop = drop | name.str.contains("ST")
    dropped = pool.filter(drop).select("symbol", "date")
    pool = pool.filter(~drop).drop("name")
    enriched_dir = data_dir / _ENRICHED
    if enriched_dir.is_dir() and any(enriched_dir.iterdir()):
        sealed = (
            pl.scan_parquet(str(enriched_dir / "*" / "part.parquet"))
            .select("symbol", "date", "consecutive_limit_ups")
            .filter(pl.col("consecutive_limit_ups") >= 1)
            .select("symbol", pl.col("date").cast(pl.Date), pl.lit(True).alias("_sealed"))
            .filter(pl.col("date").is_in(days))
            .collect()
        )
    else:
        sealed = pl.DataFrame(schema={"symbol": pl.String, "date": pl.Date, "_sealed": pl.Boolean})

    sealed = sealed.join(dropped, on=["symbol", "date"], how="anti")
    touched = pool.join(sealed, on=["symbol", "date"], how="full", coalesce=True).with_columns(
        pl.col("_sealed").fill_null(False), pl.col("_in_pool").fill_null(False))
    if exclude_st and not touched.is_empty():
        from app.services.market_reference import exclude_risk_warning

        touched = exclude_risk_warning(touched, _current_st_symbols(data_dir))

    detail = pl.col("_sealed") & pl.col("_in_pool")
    timed = detail & pl.col("first_time").is_not_null()
    # open_times 为 null (实测 2020-2023 有 6927 行) 时按「首封 != 末封」判断是否开过板:
    # 非 null 行中两者一致率 99.8%; 首末封都缺失的行不进分母
    opened = (pl.when(pl.col("open_times").is_null()).then(pl.col("first_time") != pl.col("last_time"))
              .otherwise(pl.col("open_times") > 0))
    known_open = detail & (pl.col("open_times").is_not_null()
                           | (pl.col("first_time").is_not_null() & pl.col("last_time").is_not_null()))

    def share(cond: pl.Expr, base: pl.Expr) -> pl.Expr:
        n = base.sum()
        return pl.when(n > 0).then((cond & base).sum() / n).otherwise(None)

    daily = touched.group_by("date").agg(
        pl.len().alias("touched"),
        pl.col("_sealed").sum().cast(pl.Int64).alias("sealed"),
        share(opened.fill_null(False), known_open).alias("reopen_rate"),
        share(pl.col("first_time") <= EARLY_CUTOFF, timed).alias("early_share"),
        share(pl.col("first_time") >= LATE_FROM, timed).alias("late_share"),
        share(pl.col("first_time") <= ONEWORD_CUTOFF, timed).alias("oneword_share"),
        pl.col("fd_amount").filter(detail).median().alias("fd_median"),
        (pl.col("fd_amount") / pl.col("amount")).filter(detail & (pl.col("amount") > 0)).median()
        .alias("_fd_ratio"),
        share(pl.col("_in_pool"), pl.col("_sealed")).alias("pool_coverage"),
    ).with_columns(
        # 实测 2020-05~2023-11 猫爪封单额口径不同 (约小 600 倍, 封单额/成交额中位 ~0.02%),
        # 正常年份约 10%+; 不臆测换算, 该类日子不给封单额
        pl.when(pl.col("_fd_ratio") >= FD_RATIO_MIN).then(pl.col("fd_median")).otherwise(None)
        .alias("fd_median"),
    )
    frame = (
        pl.DataFrame({"date": days}, schema={"date": pl.Date})
        .join(daily, on="date", how="left")
        .with_columns(pl.col("touched").fill_null(0).cast(pl.Int64), pl.col("sealed").fill_null(0).cast(pl.Int64))
        .with_columns(
            pl.when(pl.col("touched") > 0).then(1 - pl.col("sealed") / pl.col("touched")).otherwise(None)
            .alias("break_rate"),
            pl.when(pl.col("date") >= REOPEN_FROM).then(pl.col("reopen_rate")).otherwise(None).alias("reopen_rate"),
        )
        .sort("date")
    )
    return frame.select(_empty().columns).cast(_empty().schema)


def load_seal_quality(data_dir: Path) -> pl.DataFrame:
    """带进程内缓存的全量结果 (签名: 涨停池目录 + 参与计算的日线日期目录, ST 开关)。"""
    data_dir = Path(data_dir)
    exclude_st = _exclude_st_default()
    days = _days(data_dir)
    key = (str(data_dir), exclude_st, _signature(data_dir.joinpath(*_POOL)), _enriched_signature(data_dir, days))
    with _lock:
        hit = _cache.get(key)
    if hit is not None:
        return hit
    frame = compute_seal_quality(data_dir, exclude_st=exclude_st)
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
            out[k] = round(v, 4) if k != "fd_median" else round(v, 0)
        else:
            out[k] = v
    return out


def seal_quality_payload(data_dir: Path, start: date | None, end: date | None) -> dict:
    """接口返回: 区间内每日行 + 最新一日 (近一年分位基于全量, 不受区间影响)。"""
    frame = load_seal_quality(data_dir)
    base = {"rows": [], "latest": None, "first_date": None, "reopen_from": REOPEN_FROM.isoformat()}
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
