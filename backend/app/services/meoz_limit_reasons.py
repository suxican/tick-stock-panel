"""猫爪个股涨停归因 (开盘啦 / 韭菜公社 / 选股宝, 经 screening): 盘中实时, 收盘后按日留档。

- 当日 15:30 前: 实时读取, 进程内缓存 60 秒, 不落盘 (盘中归因会陆续补齐和修改)。
- 当日 15:30 后及历史日: 首次读取时拉取并写入
  data/meoz_archive/v1/limit_reasons/date=<日期>.parquet, 之后只读留档, 不改写。
- 归因为第三方原文, 只用于连板梯队展示, 不参与梯队、情绪或任何规则计算。
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
FIELDS = ("symbol", "kpl", "jygs", "jygs_detail", "xgb")
_SCHEMA = {name: pl.String for name in FIELDS}

_lock = threading.Lock()
_live: dict[date, tuple[float, list[dict]]] = {}


class ReasonsUnavailableError(RuntimeError):
    """猫爪插件未加载或不支持归因。"""


def path_of(data_dir: Path, day: date) -> Path:
    return data_dir / "meoz_archive" / "v1" / "limit_reasons" / f"date={day.isoformat()}.parquet"


def _fetch(day: date) -> list[dict]:
    try:
        provider = custom_sources.get_provider("meoz")
    except ValueError as exc:
        raise ReasonsUnavailableError("猫爪插件未就绪") from exc
    fetch = getattr(provider, "get_limit_reasons", None)
    if not callable(fetch):
        raise ReasonsUnavailableError("当前猫爪插件版本不支持涨停归因")
    rows = fetch(day)
    return [{name: row.get(name) for name in FIELDS} for row in rows if row.get("symbol")]


def read(data_dir: Path, day: date) -> dict:
    """返回 {date, source: live|archive, final, rows}。day 晚于今天抛 ValueError。"""
    now = cn_now()
    if day > now.date():
        raise ValueError("不能查询未来日期")
    final = day < now.date() or now.time() >= FINAL_AT
    if not final:
        with _lock:
            hit = _live.get(day)
        if hit and time_mod.monotonic() - hit[0] < LIVE_TTL_S:
            return {"date": day.isoformat(), "source": "live", "final": False, "rows": hit[1]}
        rows = _fetch(day)
        with _lock:
            _live.clear()
            _live[day] = (time_mod.monotonic(), rows)
        return {"date": day.isoformat(), "source": "live", "final": False, "rows": rows}
    path = path_of(data_dir, day)
    if not path.exists():
        rows = _fetch(day)  # 不持锁请求网络
        if rows:
            frame = pl.DataFrame(rows, schema=_SCHEMA).unique("symbol", keep="first")
            with _lock:
                if not path.exists():  # 并发的另一路已写入, 首版保留
                    path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_parquet(frame, path)
        else:
            return {"date": day.isoformat(), "source": "archive", "final": True, "rows": []}
    rows = pl.read_parquet(path).select(list(FIELDS)).to_dicts()
    return {"date": day.isoformat(), "source": "archive", "final": True, "rows": rows}


def reset_for_tests() -> None:
    with _lock:
        _live.clear()
