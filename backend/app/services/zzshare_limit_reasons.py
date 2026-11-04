"""导入 ZZShare 的盘后涨停解释，按交易日保留初次取得的有效快照。"""

from __future__ import annotations

import re
from datetime import date, time
from pathlib import Path
from threading import Lock

import polars as pl

from app.data_providers import custom as custom_sources
from app.market_time import cn_now
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, write_ext_parquet

TABLE_ID = "zzshare_limit_reasons"
_DESCRIPTION = "ZZShare 盘后涨停归因。每股每天一行，首次有效抓取后保留；抓取时间不代表发布时间。"
_FIELDS = (
    ("symbol", "string", "股票代码"),
    ("name", "string", "名称"),
    ("trade_date", "string", "归属交易日"),
    ("reason", "string", "涨停原因"),
    ("source_rows", "int", "原始条目数"),
    ("source", "string", "来源"),
    ("fetched_at", "string", "首次抓取时间（北京）"),
)
_WRITE_LOCK = Lock()


class SnapshotConflictError(RuntimeError):
    """目标表被其他配置占用，或已存快照无法核验。"""


class SourceUnavailableError(RuntimeError):
    """插件没有加载。"""


class LimitReasonsSourceError(RuntimeError):
    """上游返回异常或不完整，不保存部分结果。"""


def _stock_symbol(value: object) -> str:
    match = (
        re.fullmatch(r"([0-9]{6})(?:\.(SH|SZ|BJ))?", value.strip().upper())
        if isinstance(value, str)
        else None
    )
    if match:
        code, suffix = match.groups()
        if code.startswith(("60", "68")):
            exchange = "SH"
        elif code.startswith(("00", "30")):
            exchange = "SZ"
        elif code.startswith(("4", "8", "92")):
            exchange = "BJ"
        else:
            raise LimitReasonsSourceError("涨停归因包含无法识别的 A 股代码")
        if suffix is None or suffix == exchange:
            return f"{code}.{exchange}"
    raise LimitReasonsSourceError("涨停归因包含无效代码或交易所后缀")


def _stock_rows(raw_rows: list, day: str) -> pl.DataFrame:
    groups: dict[str, dict] = {}
    for raw in raw_rows:
        if not isinstance(raw, dict) or raw.get("date") != day:
            raise LimitReasonsSourceError("涨停归因日期与请求不一致")
        symbol = _stock_symbol(raw.get("stock_code"))
        reason = raw.get("reason")
        name = raw.get("stock_name")
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or not isinstance(name, str)
            or not name.strip()
        ):
            raise LimitReasonsSourceError("涨停归因缺少名称或原因")
        stock = groups.setdefault(symbol, {"name": name.strip(), "reasons": {}, "source_rows": 0})
        stock["reasons"][reason.strip()] = None
        stock["source_rows"] += 1

    fetched_at = cn_now().isoformat(timespec="seconds")
    return pl.DataFrame(
        [
            {
                "symbol": symbol,
                "name": stock["name"],
                "trade_date": day,
                "reason": "；".join(stock["reasons"]),
                "source_rows": stock["source_rows"],
                "source": "zzshare",
                "fetched_at": fetched_at,
            }
            for symbol, stock in groups.items()
        ]
    )


def _config(data_dir: Path) -> ExtConfig | None:
    store = ExtConfigStore(data_dir)
    if not (data_dir / "ext_data" / TABLE_ID).exists():
        # 首次建表前让老版本配置沿现有路径迁移；提前建新目录会使 load_all 跳过迁移。
        if not any(
            (data_dir / name).exists() for name in ("ext_configs.json", "ext_configs.json.bak")
        ):
            return None
        store.load_all()
        if not (data_dir / "ext_data" / TABLE_ID).exists():
            return None
    config = store.get(TABLE_ID)
    if (
        not config
        or config.id != TABLE_ID
        or config.description != _DESCRIPTION
        or config.mode != "timeseries"
        or config.pull
        or config.market_level
        or config.symbol_map
        or config.code_map
        or [(f.name, f.dtype) for f in config.fields]
        != [(name, dtype) for name, dtype, _ in _FIELDS]
    ):
        raise SnapshotConflictError("同名扩展表已有其他配置或配置不可读，请检查扩展数据")
    return config


def _result(day: str, state: str, raw_rows: int, stocks: int) -> dict:
    return {
        "table_id": TABLE_ID,
        "date": day,
        "state": state,
        "raw_rows": raw_rows,
        "stocks": stocks,
    }


def _cached(data_dir: Path, day: str) -> dict | None:
    path = data_dir / "ext_data" / TABLE_ID / "timeseries" / f"date={day}" / "part.parquet"
    if not path.exists():
        return None
    try:
        frame = pl.read_parquet(path, columns=[name for name, _, _ in _FIELDS])
        if (
            frame.is_empty()
            or any(frame.null_count().row(0))
            or frame.schema
            != {name: pl.Int64 if dtype == "int" else pl.String for name, dtype, _ in _FIELDS}
            or frame["symbol"].n_unique() != len(frame)
            or frame["source_rows"].min() < 1
            or not (frame["trade_date"] == day).all()
            or not (frame["source"] == "zzshare").all()
        ):
            raise ValueError("invalid saved snapshot")
        return _result(day, "cached", int(frame["source_rows"].sum()), len(frame))
    except (OSError, ValueError, TypeError, pl.exceptions.PolarsError) as exc:
        raise SnapshotConflictError("该日期已有快照但无法核验，请检查扩展数据") from exc


def sync_limit_reasons(data_dir: Path, day: date) -> dict:
    now = cn_now()
    if day > now.date() or (day == now.date() and now.time() < time(15)):
        raise ValueError("请选择已经收盘的日期")
    day_iso = day.isoformat()
    with _WRITE_LOCK:
        config = _config(data_dir)
        if config and (saved := _cached(data_dir, day_iso)):
            return saved
    try:
        provider = custom_sources.get_provider("zzshare")
    except ValueError as exc:
        raise SourceUnavailableError("ZZShare 插件未就绪，请检查数据源配置") from exc
    fetch = getattr(provider, "get_limit_reasons", None)
    if not callable(fetch):
        raise SourceUnavailableError("当前数据源缺少 ZZShare 涨停归因接口")
    try:
        raw_rows = fetch(day_iso)
    except RuntimeError as exc:
        raise LimitReasonsSourceError("ZZShare 请求失败，请稍后重试或检查数据源配置") from exc
    if not isinstance(raw_rows, list):
        raise LimitReasonsSourceError("涨停归因响应格式错误")
    if not raw_rows:
        return _result(day_iso, "no_data", 0, 0)
    frame = _stock_rows(raw_rows, day_iso)

    # 拉取时不持锁。并发返回时重新检查，迟到的响应不能覆写已生效的首版快照。
    with _WRITE_LOCK:
        config = _config(data_dir)
        if config:
            if saved := _cached(data_dir, day_iso):
                return saved
        else:
            config = ExtConfig(
                TABLE_ID,
                "ZZShare 涨停归因",
                "timeseries",
                [ExtField(*field) for field in _FIELDS],
                description=_DESCRIPTION,
            )
            ExtConfigStore(data_dir).upsert(config)
        write_ext_parquet(frame, config, data_dir, snapshot_date=day)
    return _result(day_iso, "synced", len(raw_rows), len(frame))
