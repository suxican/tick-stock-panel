"""猫爪当前快照的盘后留档: 只能当天取当天存的接口, 按交易日写一次、不改写。

留档 data/meoz_archive/v1/<dataset>/date=<YYYY-MM-DD>.parquet, 字段保留猫爪原名与单位,
另加 fetched_at (北京时间)。题材成分、精选成分、监管名单等接口只返回当前快照,
历史无法补拉, 所以只在当日收盘后保存当日, 不接受补存过去日期。
留档只供后续时点成分、风险状态等功能读取, 本模块不参与计算。
"""
from __future__ import annotations

import json
import logging
from datetime import date, time
from pathlib import Path
from threading import Lock

import polars as pl

from app.data_providers import custom as custom_sources
from app.market_time import cn_now
from app.services.fs_utils import atomic_write_parquet

logger = logging.getLogger(__name__)

PROVIDER = "meoz"
ROOT = ("meoz_archive", "v1")
# 与 plugins.meoz.provider.ARCHIVE_DATASETS 一致; 这里单列, 插件未加载时状态页仍能列出
DATASETS = ("theme_catalog", "theme_members", "jx_members", "thememinute_jx",
            "themefundflow_jx", "theme_hot_kp", "point_monitor", "anomaly_forecast")
READY_AT = time(15, 30)

_write_lock = Lock()
_last_run: dict[Path, dict] = {}


class ArchiveUnavailableError(RuntimeError):
    """猫爪插件未加载或不支持留档。"""


def _path(data_dir: Path, dataset: str, day: date) -> Path:
    return data_dir.joinpath(*ROOT, dataset, f"date={day.isoformat()}.parquet")


def _frame(rows: list[dict], fetched_at: str) -> pl.DataFrame:
    # 列表/字典字段转 JSON 文本, 其余标量原样; 全部按字符串推断以免混合类型整批失败
    columns = list(dict.fromkeys(key for row in rows for key in row))
    flat = [{key: _scalar(row.get(key)) for key in columns} for row in rows]
    frame = pl.DataFrame(flat, schema={key: pl.String for key in columns}, orient="row")
    return frame.with_columns(pl.lit(fetched_at).alias("fetched_at"))


def _scalar(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _provider():
    try:
        provider = custom_sources.get_provider(PROVIDER)
    except ValueError as exc:
        raise ArchiveUnavailableError("猫爪插件未就绪，请检查数据源配置") from exc
    fetch = getattr(provider, "get_archive_rows", None)
    if not callable(fetch):
        raise ArchiveUnavailableError("当前猫爪插件版本不支持留档")
    return fetch


def archive_today(data_dir: Path, datasets: tuple[str, ...] = DATASETS) -> dict:
    """保存今天 (北京) 的快照。须在 15:30 之后; 已存的数据集跳过, 单个失败不影响其余。"""
    now = cn_now()
    if now.time() < READY_AT:
        raise ValueError("请在当日 15:30 收盘数据更新后留档")
    unknown = set(datasets) - set(DATASETS)
    if unknown:
        raise ValueError(f"未知留档数据集: {', '.join(sorted(unknown))}")
    day = now.date()
    fetch = _provider()
    results: dict[str, dict] = {}
    for dataset in datasets:
        path = _path(data_dir, dataset, day)
        if path.exists():
            results[dataset] = {"state": "cached"}
            continue
        try:
            rows = fetch(dataset, day)  # 不持锁请求网络
        except Exception as exc:  # noqa: BLE001 -- 单个数据集失败不阻断其余, 原因写入结果
            logger.warning("猫爪留档 %s 失败: %s", dataset, exc)
            results[dataset] = {"state": "error", "error": str(exc)[:200]}
            continue
        if not rows:
            results[dataset] = {"state": "empty"}
            continue
        frame = _frame(rows, now.isoformat(timespec="seconds"))
        with _write_lock:
            if path.exists():  # 并发的另一轮已写入, 首版保留
                results[dataset] = {"state": "cached"}
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_parquet(frame, path)
        results[dataset] = {"state": "saved", "rows": frame.height}
    run = {"date": day.isoformat(), "finished_at": cn_now().isoformat(timespec="seconds"),
           "results": results}
    _last_run[data_dir] = run
    return run


def status(data_dir: Path) -> dict:
    """每个数据集已留档的天数与最近日期, 以及本进程最近一次运行结果。"""
    datasets = {}
    for dataset in DATASETS:
        folder = data_dir.joinpath(*ROOT, dataset)
        days = sorted(p.stem.removeprefix("date=") for p in folder.glob("date=*.parquet")) \
            if folder.is_dir() else []
        datasets[dataset] = {"days": len(days), "first": days[0] if days else None,
                             "latest": days[-1] if days else None}
    return {"datasets": datasets, "last_run": _last_run.get(data_dir)}
