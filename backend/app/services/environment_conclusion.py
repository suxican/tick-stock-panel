"""市场环境结论: 当天落在"情绪三档 x 资金档位"哪一格, 用户对这一格的预设, 以及证据。

系统不生成仓位建议: 仓位上限与打法全部来自用户在偏好里预设的 9 格。这里只做三件事:
1. 按 regime_history 最新一行定位格子(情绪三档复用校准的 MATRIX_SENTIMENT);
2. 与博弈页当日盘后计划的市场闸门 allocation.total_cap 取小值, 生效上限永不高于闸门;
3. 附最新校准归档中该格的次日全市场等权统计(样本内代理, 不是个股或可成交收益)。

单位: 预设与输出的 *_cap_pct 为 0-100 整数百分数; 博弈报告 total_cap 为小数, 在此换算。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, time
from pathlib import Path

from app.market_time import CN_TZ
from app.services.market_game_calibration import MATRIX_SENTIMENT
from app.services.market_game_credibility import CalibrationArchive
from app.services.regime_builder import load_regime_history

logger = logging.getLogger(__name__)

_VOLUMES = ("expand", "flat", "shrink")
_REPORT_NAME = re.compile(r"mg_[0-9a-f]{32}")


def _latest_row(data_dir: Path) -> dict | None:
    df = load_regime_history(data_dir)
    if df.is_empty() or "date" not in df.columns:
        return None
    return df.sort("date").tail(1).to_dicts()[0]


def _locate(row: dict) -> tuple[str | None, str | None, str | None]:
    """返回 (情绪档, 资金档, 缺失原因)。"""
    tier = MATRIX_SENTIMENT.get(row.get("state"))
    if tier is None:
        return None, None, "环境分缺失"
    volume = row.get("capital_volume")
    if volume not in _VOLUMES:
        return tier, None, "资金档位未计算"
    return tier, volume, None


def _gate(data_dir: Path, day: str) -> dict | None:
    """当日博弈报告的市场闸门; 同日多份取 created_at 最新, 读不了的报告跳过。

    报告文件含全市场快照, 体积大; 只读 mtime 不早于该交易日北京时间零点的文件,
    当日计划只可能在当日或之后生成, 不会漏掉。
    """
    root = Path(data_dir) / "user_data" / "market_game"
    if not root.is_dir():
        return None
    floor = datetime.combine(datetime.fromisoformat(day).date(), time(0), CN_TZ).timestamp()
    best = None
    for path in root.glob("mg_*.json"):
        if not _REPORT_NAME.fullmatch(path.stem):
            continue
        try:
            if path.stat().st_mtime < floor:
                continue
            report = json.loads(path.read_text(encoding="utf-8"))["report"]
            if report.get("as_of") != day:
                continue
            cap = report["allocation"]["total_cap"]
            if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not 0 <= cap <= 1:
                continue
            created = str(report.get("created_at") or "")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.debug("skip market game report %s: %s", path.name, exc)
            continue
        if best is None or created > best[0]:
            best = (created, {"cap_pct": round(cap * 100), "report_id": path.stem[3:], "as_of": day})
    return best[1] if best else None


def _evidence(data_dir: Path, cell: str) -> tuple[dict | None, str | None]:
    latest = CalibrationArchive(data_dir).latest()
    if not latest:
        return None, None
    cells = latest.get("cells")
    if not isinstance(cells, dict):
        return None, latest.get("created_at")
    return cells.get(cell), latest.get("created_at")


def build_conclusion(data_dir: Path, *, plan: dict | None = None) -> dict:
    if plan is None:
        from app.services import preferences

        plan = preferences.get_env_matrix_plan()
    out = {
        "date": None, "cell": None, "sentiment": None, "volume": None, "missing_reason": None,
        "plan": None, "gate": None, "effective_cap_pct": None, "limited_by": None,
        "evidence": None, "evidence_as_of": None,
    }
    row = _latest_row(data_dir)
    if row is None:
        out["missing_reason"] = "暂无环境历史"
        return out
    day = str(row["date"])
    tier, volume, reason = _locate(row)
    out.update(date=day, sentiment=tier, volume=volume, missing_reason=reason)
    if reason is not None:
        return out
    cell = f"{tier}_{volume}"
    preset = plan.get(cell)
    gate = _gate(data_dir, day)
    evidence, evidence_as_of = _evidence(data_dir, cell)
    out.update(cell=cell, plan=preset, gate=gate, evidence=evidence, evidence_as_of=evidence_as_of)
    if preset is not None:
        mine = preset["cap_pct"]
        if gate is None or mine < gate["cap_pct"]:
            out.update(effective_cap_pct=mine, limited_by="plan")
        elif mine == gate["cap_pct"]:
            out.update(effective_cap_pct=mine, limited_by="both")
        else:
            out.update(effective_cap_pct=gate["cap_pct"], limited_by="gate")
    return out
