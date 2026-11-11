"""单日市场状态(情绪分) —— 复盘与看板头部共用的唯一口径。

情绪分只认 regime 口径(regime_builder.classify_state, 4 维: 赚钱/投机/抗跌/趋势):
它与 Regime 页、回测环境过滤、周期阶段否决是同一数值, 且有逐日历史可比。

- 复盘日已入 regime_history (盘后管道写入) → 直接取表中 score/state/子分, source="regime";
- 尚未入表 (盘中或管道未跑) → 用总览的同名指标调用同一 classify_state 估算,
  source="estimate"。估算与盘后值的差异仅来自样本(实时行情、五档修正、MA20 有效样本),
  属暂定值。

单位在本模块边界统一转换(总览给百分数, regime 要比例/小数), 见 overview_metrics。
"""
from __future__ import annotations

import logging
import math
from datetime import date
from pathlib import Path

import polars as pl

from app.services import regime_builder

logger = logging.getLogger(__name__)

SUBSCORE_KEYS = ("profit", "speculation", "resilience", "trend")
REGIME_INDEX_SYMBOL = "000001.SH"   # 与 regime_builder._load_index_pct 默认指数一致


def _finite(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def overview_metrics(
    *,
    up_pct: float,
    down_pct: float,
    avg_pct: float,
    median_pct: float,
    strong_up_pct: float,
    strong_down_pct: float,
    limit_up: int,
    broken: int,
    max_boards: int,
    index_change_pct: float | None,
    above_ma20_pct: float,
) -> dict:
    """总览口径 → classify_state 需要的 metrics。

    输入单位(总览): up/down/strong_*_pct、above_ma20_pct、index_change_pct 为百分数
    (1.2 = 1.2%); avg_pct/median_pct 为小数。输出单位同 regime_builder._aggregate_daily:
    seal_rate/above_ma20_pct/index_pct 为比例或小数, 其余占比为百分数。
    """
    sealed_pool = limit_up + broken
    return {
        "up_pct": up_pct,
        "down_pct": down_pct,
        "avg_pct": avg_pct,
        "median_pct": median_pct,
        "strong_up_pct": strong_up_pct,
        "strong_down_pct": strong_down_pct,
        "strong_diff_pct": strong_up_pct - strong_down_pct,
        "limit_up": limit_up,
        # 无涨停/炸板时 regime 取中性 0.5 (而非 0), 保持同口径
        "seal_rate": limit_up / sealed_pool if sealed_pool > 0 else 0.5,
        "max_consecutive": max_boards,
        "index_pct": (index_change_pct or 0.0) / 100,
        "above_ma20_pct": above_ma20_pct / 100,
    }


def index_change_pct(indices: list[dict], symbol: str = REGIME_INDEX_SYMBOL) -> float | None:
    """总览指数列表中取 regime 所用指数的涨跌幅(百分数)。"""
    for idx in indices or []:
        if idx.get("symbol") == symbol:
            return _finite(idx.get("change_pct"))
    return None


def _from_regime_row(row: dict) -> dict | None:
    score = _finite(row.get("score"))
    state = row.get("state")
    if score is None or not state:
        return None
    subs = {k: _finite(row.get(f"{k}_score")) for k in SUBSCORE_KEYS}
    return {
        "score": round(score),
        "state": state,
        "subscores": {k: round(v) for k, v in subs.items() if v is not None},
    }


def _estimate(metrics: dict) -> dict:
    state, score = regime_builder.classify_state(metrics)
    subs = regime_builder._compute_subscores(metrics)
    return {
        "score": score,
        "state": state,
        "subscores": {k: round(subs[k]) for k in SUBSCORE_KEYS},
    }


def resolve_regime_state(data_dir: Path | None, as_of: date, metrics: dict | None) -> dict | None:
    """复盘日市场状态: regime_history 当日行优先, 否则用 metrics 估算; 都不可用返回 None。

    返回 {score, state, label, source: "regime"|"estimate", date, subscores}。
    """
    base = None
    source = "regime"
    if data_dir is not None:
        try:
            df = regime_builder.load_regime_history(Path(data_dir))
            if not df.is_empty() and "date" in df.columns:
                hit = df.filter(pl.col("date") == as_of)
                if not hit.is_empty():
                    base = _from_regime_row(hit.row(0, named=True))
        except Exception as e:  # noqa: BLE001
            logger.debug("regime_state: regime history unavailable: %s", e)
    if base is None:
        if metrics is None:
            return None
        base = _estimate(metrics)
        source = "estimate"
    return {
        **base,
        "label": regime_builder.STATE_LABELS.get(base["state"], base["state"]),
        "source": source,
        "date": as_of.isoformat(),
    }
