"""按已确定的股票成员和交易日，聚合本地分钟数据的等权曲线及全市场对照线。"""
from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

import polars as pl

from app.services.sector_rotation import minute_percentages, previous_daily_partition

_FFILL_CAP = 2000  # 超大板块避免展开 成分股 × 全时间轴。


def _signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def member_intraday(data_dir: Path, day: date, member_codes: list[str]) -> dict:
    target = day.isoformat()
    minute = data_dir / "kline_minute" / f"date={target}" / "part.parquet"
    try:
        minute_version = _signature(minute)
    except FileNotFoundError:
        return {"status": "no_data", "reason": "minute_missing", "date": target, "points": []}
    members = tuple(sorted(set(member_codes)))
    if not members:
        return {"status": "empty", "reason": "no_members", "date": target, "points": []}
    previous = previous_daily_partition(data_dir, target)
    try:
        prior_version = _signature(previous) if previous else None
    except FileNotFoundError:
        previous = prior_version = None
    return _member_chart(data_dir, target, members, previous, minute_version, prior_version)


@lru_cache(maxsize=64)
def _member_chart(data_dir: Path, target: str, members: tuple[str, ...], previous: Path | None,
                  _minute_version: tuple, _prior_version: tuple | None) -> dict:
    # 缓存仅保留曲线。分钟写入、前收回补和名单变更分别由文件签名与代码集合失效。
    joined, basis, _ = minute_percentages(data_dir / "kline_minute", target, previous)
    if joined is None:
        return {"status": "no_data", "reason": basis, "date": target, "points": []}

    market = joined.group_by("datetime").agg(pl.col("_pct").mean().alias("market"))
    member_bars = joined.filter(pl.col("_bare").is_in(members))
    covered = member_bars["_bare"].n_unique()
    result = {"date": target, "member_count": len(members), "members_with_minute": covered}
    if not covered:
        return {**result, "status": "empty", "reason": "no_member_bars", "points": []}
    if covered <= _FFILL_CAP:
        grid = member_bars.select(pl.col("_bare").unique()).join(market.select("datetime"), how="cross")
        member_bars = (
            grid.join(member_bars.select("_bare", "datetime", "_pct"), on=["_bare", "datetime"], how="left")
            .sort("_bare", "datetime")
            .with_columns(pl.col("_pct").forward_fill().over("_bare"))
        )
    sector = member_bars.group_by("datetime").agg(pl.col("_pct").mean().alias("sector"))
    points = market.join(sector, on="datetime", how="left").sort("datetime").select(
        pl.col("datetime").dt.strftime("%H:%M").alias("time"),
        pl.col("sector").round(4), pl.col("market").round(4),
    ).to_dicts()
    return {**result, "status": "ok", "basis": basis, "points": points}
