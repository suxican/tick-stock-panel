"""交易所参考数据 (涨跌停价 / ST 名单 / 停牌名单 / 涨停池) 的按日同步、历史回填与状态。

每个能力按能力路由矩阵取生效源, 一个交易日一张表:
data/market_reference/v1/<dataset>/date=<YYYY-MM-DD>.parquet, 列为 data_providers.schemas
中的源中立契约。表存在即该日完整, 写入后不改写。生效源不可用时不同步, 读取方回退本地推算
(理论涨跌停价、当前名称推断 ST), 因此任何一个源失效都不会让现有计算中断。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, time, timedelta
from pathlib import Path

import polars as pl

from app.data_providers import custom as custom_sources
from app.data_providers.capabilities import resolve_capability_sources
from app.data_providers.schemas import (
    AUCTION_SCHEMA,
    LIMIT_EVENT_SCHEMA,
    PRICE_LIMIT_SCHEMA,
    ST_STATUS_SCHEMA,
    SUSPENSION_SCHEMA,
)
from app.market_time import cn_now
from app.price_limits import polars_price_limit_pct
from app.services.fs_utils import atomic_write_parquet

logger = logging.getLogger(__name__)

ROOT = ("market_reference", "v1")
MAX_CONSECUTIVE_ERRORS = 5  # 回填连续失败即停 (Key 失效、额度耗尽时不继续消耗调用)


@dataclass(frozen=True)
class Spec:
    method: str
    schema: dict
    first: date  # 已核实的上游最早可用交易日 (2026-11 猫爪实测)
    ready: time  # 当日数据最早可取的北京时间


DATASETS: dict[str, Spec] = {
    "price_limits": Spec("get_price_limits", PRICE_LIMIT_SCHEMA, date(2020, 1, 2), time(9, 0)),
    "st_status": Spec("get_st_status", ST_STATUS_SCHEMA, date(2021, 1, 4), time(9, 0)),
    "limit_events": Spec("get_limit_events", LIMIT_EVENT_SCHEMA, date(2017, 1, 3), time(15, 30)),
    "suspensions": Spec("get_suspensions", SUSPENSION_SCHEMA, date(2020, 1, 2), time(9, 0)),
    # 集合竞价 09:25 定版; 情绪周期「竞价情绪」与回测使用 (猫爪 daily_auc 2019-01 起)
    "auction": Spec("get_auction_daily", AUCTION_SCHEMA, date(2019, 1, 2), time(9, 26)),
}

_write_lock = threading.Lock()
_backfill_lock = threading.Lock()
_backfill: dict = {}  # 当前/最近一次回填进度 (进程内)


class ReferenceUnavailableError(RuntimeError):
    """生效源不可用或未实现该数据集。"""


class ReferenceDataError(RuntimeError):
    """上游返回不完整或不符合契约, 不落盘。"""


def path_of(data_dir: Path, dataset: str, day: date) -> Path:
    return data_dir.joinpath(*ROOT, dataset, f"date={day.isoformat()}.parquet")


def _fetcher(dataset: str) -> tuple[str, Callable[[date], pl.DataFrame]]:
    row = next((r for r in resolve_capability_sources("none") if r["id"] == dataset), None)
    if row is None or not row["usable"]:
        source = row["source_display"] if row else dataset
        raise ReferenceUnavailableError(f"{dataset} 的数据源 {source} 当前不可用")
    try:
        provider = custom_sources.get_provider(row["source"])
    except ValueError as exc:
        raise ReferenceUnavailableError(f"{dataset} 的数据源 {row['source']} 未加载") from exc
    fetch = getattr(provider, DATASETS[dataset].method, None)
    if not callable(fetch):
        raise ReferenceUnavailableError(f"{row['source']} 未实现 {dataset}")
    return row["source"], fetch


def _validate(frame: object, dataset: str, day: date) -> pl.DataFrame:
    if not isinstance(frame, pl.DataFrame):
        raise ReferenceDataError(f"{dataset} 返回类型错误")
    schema = DATASETS[dataset].schema
    if frame.schema != pl.Schema(schema):
        raise ReferenceDataError(f"{dataset} 字段与契约不一致")
    if frame.is_empty():
        raise ReferenceDataError(f"{dataset} {day} 无数据")
    if not (frame["date"] == day).all():
        raise ReferenceDataError(f"{dataset} 含非 {day} 的行")
    if frame["symbol"].null_count() or frame["symbol"].n_unique() != frame.height:
        raise ReferenceDataError(f"{dataset} 代码缺失或重复")
    return frame


def sync_day(data_dir: Path, dataset: str, day: date) -> dict:
    """同步一个交易日; 已有则跳过。调用方负责只传交易日。"""
    spec = DATASETS.get(dataset)
    if spec is None:
        raise ValueError(f"未知数据集: {dataset}")
    now = cn_now()
    if day > now.date() or (day == now.date() and now.time() < spec.ready):
        raise ValueError(f"{dataset} 当日数据须在 {spec.ready:%H:%M} 之后同步")
    path = path_of(data_dir, dataset, day)
    if path.exists():
        return {"state": "cached"}
    source, fetch = _fetcher(dataset)
    frame = _validate(fetch(day), dataset, day)  # 不持锁请求网络
    with _write_lock:
        if path.exists():  # 并发的另一路已写入, 首版保留
            return {"state": "cached"}
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_parquet(frame, path)
    return {"state": "saved", "rows": frame.height, "source": source}


def local_trading_days(data_dir: Path, start: date, end: date) -> list[date]:
    """本地日线分区即项目实际交易日 (与 enriched 重算同一日期集合)。"""
    days = []
    for folder in (data_dir / "kline_daily").glob("date=*"):
        try:
            day = date.fromisoformat(folder.name.removeprefix("date="))
        except ValueError:
            continue
        if start <= day <= end:
            days.append(day)
    return sorted(days)


def backfill(data_dir: Path, dataset: str, start: date | None = None, end: date | None = None,
             should_stop: Callable[[], bool] = lambda: False,
             on_progress: Callable[[dict], None] = lambda _p: None) -> dict:
    """按本地交易日逐日补齐缺失的表 (已有跳过)。连续失败 MAX_CONSECUTIVE_ERRORS 次即停。"""
    spec = DATASETS[dataset]
    now = cn_now()
    # 今天只有过了当日可取时间才纳入
    cutoff = now.date() if now.time() >= spec.ready else now.date() - timedelta(days=1)
    end = min(end or cutoff, cutoff)
    days = local_trading_days(data_dir, max(start or spec.first, spec.first), end)
    todo = [d for d in days if not path_of(data_dir, dataset, d).exists()]
    progress = {"dataset": dataset, "total": len(days), "todo": len(todo), "done": 0,
                "saved": 0, "errors": [], "state": "running", "stopped_reason": None}
    on_progress(dict(progress))
    streak = 0
    for day in todo:
        if should_stop():
            progress.update(state="stopped", stopped_reason="已手动停止")
            break
        try:
            result = sync_day(data_dir, dataset, day)
        except ReferenceUnavailableError as exc:
            progress.update(state="failed", stopped_reason=str(exc))
            break
        except Exception as exc:  # noqa: BLE001 -- 单日失败记录后继续, 连续失败才停
            streak += 1
            progress["errors"] = [*progress["errors"][-19:], {"date": day.isoformat(), "error": str(exc)[:200]}]
            if streak >= MAX_CONSECUTIVE_ERRORS:
                progress.update(state="failed", stopped_reason=f"连续 {streak} 天失败，已停止")
                break
        else:
            streak = 0
            progress["saved"] += result["state"] == "saved"
        progress["done"] += 1
        on_progress(dict(progress))
    if progress["state"] == "running":
        progress["state"] = "succeeded"
    on_progress(dict(progress))
    return progress


def start_backfill(data_dir: Path, dataset: str, start: date | None = None, end: date | None = None) -> dict:
    """后台线程回填; 同一时间只运行一个回填。"""
    if dataset not in DATASETS:
        raise ValueError(f"未知数据集: {dataset}")
    _fetcher(dataset)  # 源不可用时直接拒绝, 不起线程
    with _backfill_lock:
        if _backfill.get("state") == "running":
            raise RuntimeError("已有回填在运行")
        stop = threading.Event()
        _backfill.clear()
        _backfill.update(dataset=dataset, state="running", stop=stop)

    def update(progress: dict) -> None:
        with _backfill_lock:
            _backfill.update(progress)

    def run() -> None:
        try:
            backfill(data_dir, dataset, start, end, stop.is_set, update)
        except Exception as exc:  # 线程内异常写回状态
            logger.exception("交易所参考数据回填失败")
            update({"state": "failed", "stopped_reason": str(exc)[:200]})

    threading.Thread(target=run, name=f"market-reference-{dataset}", daemon=True).start()
    return backfill_status()


def stop_backfill() -> dict:
    with _backfill_lock:
        stop = _backfill.get("stop")
    if stop is not None:
        stop.set()
    return backfill_status()


def backfill_status() -> dict | None:
    with _backfill_lock:
        return {k: v for k, v in _backfill.items() if k != "stop"} or None


def status(data_dir: Path) -> dict:
    routes = {r["id"]: r for r in resolve_capability_sources("none")}
    datasets = {}
    for dataset, spec in DATASETS.items():
        folder = data_dir.joinpath(*ROOT, dataset)
        days = sorted(p.stem.removeprefix("date=") for p in folder.glob("date=*.parquet")) \
            if folder.is_dir() else []
        route = routes.get(dataset, {})
        datasets[dataset] = {"days": len(days), "first": days[0] if days else None,
                             "latest": days[-1] if days else None, "upstream_first": spec.first.isoformat(),
                             "source": route.get("source"), "usable": bool(route.get("usable"))}
    return {"datasets": datasets, "backfill": backfill_status()}


# ---- 读取 (供 enriched 涨跌停计算) ----
# 进程级显式启用: 应用启动与回测 worker 入口调用 enable_reader; 未启用 (含单元测试)
# 时读取方保持本地推算, 不会意外读到运行机器上的真实数据。
_reader_root: Path | None = None


def enable_reader(data_dir: Path | None) -> None:
    global _reader_root
    _reader_root = Path(data_dir) if data_dir is not None else None


def _days_in(dataset: str, start: date, end: date) -> dict[date, Path]:
    if _reader_root is None:
        return {}
    folder = _reader_root.joinpath(*ROOT, dataset)
    found: dict[date, Path] = {}
    for path in folder.glob("date=*.parquet") if folder.is_dir() else ():
        try:
            day = date.fromisoformat(path.stem.removeprefix("date="))
        except ValueError:
            continue
        if start <= day <= end:
            found[day] = path
    return found


def price_limit_reference(symbols: list[str], start: date, end: date) -> pl.DataFrame | None:
    """[start, end] 内已落盘的交易所涨跌停价: symbol, date, _ref_limit_up, _ref_limit_down, _ref_no_limit。"""
    files = _days_in("price_limits", start, end)
    if not files:
        return None
    try:
        return (
            pl.scan_parquet(list(files.values()))
            .filter(pl.col("symbol").is_in(symbols))
            .select("symbol", "date", pl.col("limit_up").alias("_ref_limit_up"),
                    pl.col("limit_down").alias("_ref_limit_down"), pl.col("no_limit").alias("_ref_no_limit"))
            .collect()
        )
    except (OSError, pl.exceptions.PolarsError):
        logger.warning("交易所涨跌停价读取失败, 回退本地推算", exc_info=True)
        return None


def st_reference(symbols: list[str], start: date, end: date) -> tuple[list[date], pl.DataFrame] | None:
    """已落盘 ST 名单覆盖的交易日, 以及这些日子里处于风险警示的 (symbol, date)。"""
    files = _days_in("st_status", start, end)
    if not files:
        return None
    try:
        pairs = (pl.scan_parquet(list(files.values())).filter(pl.col("symbol").is_in(symbols))
                 .select("symbol", "date").collect())
    except (OSError, pl.exceptions.PolarsError):
        logger.warning("ST 名单读取失败, 回退名称推断", exc_info=True)
        return None
    return sorted(files), pairs


# ---- 读取 (供持仓做T / 持仓分析按日查单只股票) ----
_DAY_TABLES = {"price_limits": "price_limits", "st": "st_status", "suspensions": "suspensions"}
_day_cache: dict = {}
_day_cache_lock = threading.Lock()


def _day_table(key: str, frame: pl.DataFrame) -> dict:
    if key == "price_limits":
        return {r["symbol"]: {"limit_up": r["limit_up"], "limit_down": r["limit_down"], "no_limit": r["no_limit"]}
                for r in frame.iter_rows(named=True)}
    if key == "st":
        return {r["symbol"]: r["st_type"] for r in frame.iter_rows(named=True)}
    return {r["symbol"]: r["timing"] for r in frame.iter_rows(named=True)}


def day_reference(day: date) -> dict:
    """某交易日已落盘的涨跌停价 / ST / 停牌名单, 按代码索引。

    某表当日未落盘 (或读取未启用、读取失败) 时该项为 None —— 表示"未知", 不是"没有"。
    按 (日期, 文件 mtime) 缓存, 盘中补同步写入新文件后自动重读。
    """
    out: dict = {key: None for key in _DAY_TABLES}
    if _reader_root is None:
        return out
    paths = {key: path_of(_reader_root, dataset, day) for key, dataset in _DAY_TABLES.items()}
    stamp = []
    for key, path in paths.items():
        try:
            stamp.append((key, path.stat().st_mtime_ns))
        except OSError:
            stamp.append((key, None))
    cache_key = (str(_reader_root), day, tuple(stamp))
    with _day_cache_lock:
        if cache_key in _day_cache:
            return dict(_day_cache[cache_key])
    for key, mtime in stamp:
        if mtime is None:
            continue
        try:
            out[key] = _day_table(key, pl.read_parquet(paths[key]))
        except (OSError, pl.exceptions.PolarsError):
            logger.warning("%s %s 读取失败, 视为未知", _DAY_TABLES[key], day, exc_info=True)
    with _day_cache_lock:
        _day_cache.clear()  # 只保留最近一份, 盘中通常只查当日
        _day_cache[cache_key] = out
    return dict(out)


def _inferred_risk_warning(symbols: list[str], start: date, end: date) -> pl.DataFrame | None:
    """无 ST 名单的交易日, 由交易所涨跌停价反推风险警示: 主板类股票 (2026-07-06 前 ST 为 5%)
    当日涨跌幅只有约 5% 即为风险警示。产出该股该日有交易所价的全部行: symbol, date, _px_st。"""
    prices = price_limit_reference(symbols, start, end)
    if prices is None or prices.is_empty():
        return None
    up, down = pl.col("_ref_limit_up"), pl.col("_ref_limit_down")
    # 若该股该日戴帽会适用 5%, 即为主板类规则; 用同一权威规则判断板块与日期
    legacy_st_rule = polars_price_limit_pct(pl.col("symbol"), pl.col("date"), pl.lit(True)) < 0.06
    return prices.select(
        pl.col("symbol").str.to_uppercase().alias("_st_sym"), "date",
        (legacy_st_rule & ((up - down) / (up + down) < 0.075)).fill_null(False).alias("_px_st"),
    )


def exclude_risk_warning(df: pl.DataFrame, current_symbols) -> pl.DataFrame:
    """剔除风险警示股, 按可得的最权威口径逐日判断:
    有当日 ST 名单 → 按名单; 否则有交易所涨跌停价 → 按价格反推; 都没有 → 按当前名单 (原口径)。
    """
    current = sorted({str(s).upper() for s in current_symbols})
    if df.is_empty() or "symbol" not in df.columns:
        return df
    sym = pl.col("symbol").str.to_uppercase()
    if "date" not in df.columns or df.schema["date"] != pl.Date:
        return df.filter(~sym.is_in(current)) if current else df
    symbols, start, end = df["symbol"].unique().to_list(), df["date"].min(), df["date"].max()
    st = st_reference(symbols, start, end)
    inferred = _inferred_risk_warning(symbols, start, end)
    if st is None and inferred is None:
        return df.filter(~sym.is_in(current)) if current else df
    out = df.with_columns(sym.alias("_st_sym"))
    days: list[date] = []
    if st is not None:
        days, pairs = st
        out = out.join(pairs.select(pl.col("symbol").str.to_uppercase().alias("_st_sym"), "date",
                                    pl.lit(True).alias("_st_hit")), on=["_st_sym", "date"], how="left")
    else:
        out = out.with_columns(pl.lit(None, dtype=pl.Boolean).alias("_st_hit"))
    if inferred is not None:
        out = out.join(inferred, on=["_st_sym", "date"], how="left")
    else:
        out = out.with_columns(pl.lit(None, dtype=pl.Boolean).alias("_px_st"))
    keep = (
        pl.when(pl.col("date").is_in(days)).then(pl.col("_st_hit").is_null())
        .when(pl.col("_px_st").is_not_null()).then(~pl.col("_px_st"))
        .otherwise(~pl.col("_st_sym").is_in(current))
    )
    return out.filter(keep).drop("_st_sym", "_st_hit", "_px_st")
