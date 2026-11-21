"""猫爪板块原因 (theme_reason: 选股宝 / 韭菜公社): 盘中实时, 收盘后按日留档。

- 当日 15:30 前: 实时读取, 进程内缓存 60 秒, 不落盘 (原因会陆续补齐)。
- 当日 15:30 后及历史日: 首次读取并写入 data/meoz_archive/v1/theme_reason/date=<日期>.parquet,
  之后只读留档, 不改写。接口可回查到 2026-01-05, 更早日期返回空。
- 任一来源取数失败时不留档 (避免把残缺结果定版), 结果带 unavailable_sources, 页面显示「来源不可用」。
- 题材 ID 是各来源自己的体系, 与本地板块不同源; 原文只用于展示, 不参与主线评分或阶段判定。
- 接口没有更新时间字段, 证据时间只有 fetched_at (获取时间, 北京)。
"""
from __future__ import annotations

import logging
import threading
import time as time_mod
from datetime import date, time
from pathlib import Path

import polars as pl

from app.data_providers import custom as custom_sources
from app.market_time import cn_now
from app.services.fs_utils import atomic_write_parquet

logger = logging.getLogger(__name__)

FINAL_AT = time(15, 30)
LIVE_TTL_S = 60.0
HISTORY_START = date(2026, 1, 5)
FIELDS = ("source", "theme_id", "name", "reason")
_SCHEMA = {name: pl.String for name in FIELDS}

_lock = threading.Lock()
_live: dict[date, tuple[float, dict]] = {}


class ThemeReasonsUnavailableError(RuntimeError):
    """猫爪插件未加载或不支持板块原因。"""


def path_of(data_dir: Path, day: date) -> Path:
    return data_dir / "meoz_archive" / "v1" / "theme_reason" / f"date={day.isoformat()}.parquet"


def _fetch(day: date) -> dict:
    try:
        provider = custom_sources.get_provider("meoz")
    except ValueError as exc:
        raise ThemeReasonsUnavailableError("猫爪插件未就绪") from exc
    fetch = getattr(provider, "get_theme_reasons", None)
    if not callable(fetch):
        raise ThemeReasonsUnavailableError("当前猫爪插件版本不支持板块原因")
    result = fetch(day)
    rows = [{name: row.get(name) for name in FIELDS} for row in result["rows"]]
    return {"rows": rows, "failed": list(result["failed"])}


def _packet(day: date, source: str, final: bool, rows: list[dict], failed: list[str], fetched_at: str | None) -> dict:
    return {"date": day.isoformat(), "source": source, "final": final, "rows": rows,
            "unavailable_sources": failed, "fetched_at": fetched_at}


def read(data_dir: Path, day: date) -> dict:
    """返回 {date, source: live|archive, final, rows, unavailable_sources, fetched_at}。"""
    now = cn_now()
    if day > now.date():
        raise ValueError("不能查询未来日期")
    final = day < now.date() or now.time() >= FINAL_AT
    stamp = now.isoformat(timespec="seconds")
    if day < HISTORY_START:
        return _packet(day, "archive", True, [], [], None)
    if not final:
        with _lock:
            hit = _live.get(day)
        if hit and time_mod.monotonic() - hit[0] < LIVE_TTL_S:
            return hit[1]
        result = _fetch(day)
        packet = _packet(day, "live", False, result["rows"], result["failed"], stamp)
        with _lock:
            _live.clear()
            _live[day] = (time_mod.monotonic(), packet)
        return packet
    path = path_of(data_dir, day)
    if not path.exists():
        result = _fetch(day)  # 不持锁请求网络
        if result["failed"] or not result["rows"]:
            return _packet(day, "live", True, result["rows"], result["failed"], stamp)
        frame = pl.DataFrame(result["rows"], schema=_SCHEMA).with_columns(pl.lit(stamp).alias("fetched_at"))
        with _lock:
            if not path.exists():  # 并发的另一路已写入, 首版保留
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_parquet(frame, path)
    saved = pl.read_parquet(path)
    return _packet(day, "archive", True, saved.select(list(FIELDS)).to_dicts(), [], saved["fetched_at"][0])


LATEST_LOOKBACK_DAYS = 10


def read_latest(data_dir: Path, day: date) -> dict:
    """当日无板块原因 (休市日 / 开盘前尚未生成) 时, 向前回退到最近一个有数据的日期。

    返回包的 date 字段即实际日期; 任一来源失败时不回退 (残缺结果不能当作「当日没有」)。
    周末直接跳过, 法定休市日靠「空结果」自然跳过。
    """
    packet = read(data_dir, day)
    probe = day
    for _ in range(LATEST_LOOKBACK_DAYS):
        if packet["rows"] or packet["unavailable_sources"]:
            break
        probe = date.fromordinal(probe.toordinal() - 1)
        if probe < HISTORY_START:
            break
        if probe.weekday() >= 5:
            continue
        packet = read(data_dir, probe)
    if not packet["rows"]:
        return read(data_dir, day)
    if packet["date"] != day.isoformat() and day.weekday() < 5:
        # 工作日回退: 可能只是今天还没出数据, 保持非定版让前端继续轮询, 出数后自动切回今天
        return {**packet, "final": False}
    return packet


def reset_for_tests() -> None:
    with _lock:
        _live.clear()
