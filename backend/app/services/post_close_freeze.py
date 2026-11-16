"""盘后定版: 定时盘后管道成功后, 补齐当日博弈计划并存档当天的环境结论。

1. 环境历史最新日 D 没有 as_of == D 的博弈计划时, 以"最新盘后"模式生成一份,
   风险参数沿用最近一份报告的 requested_risk(用户在博弈页最后一次使用的设置);
   用户已手动生成或正在生成时不重复。计划提供环境结论所需的纯风险闸门。
2. 用当时的预设与闸门算出环境结论, 追加一版存档(trigger="pipeline")。

全程软失败: 任何一步出错只记日志并在返回值中说明, 不影响盘后管道结果。
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.services import environment_conclusion, environment_conclusion_archive
from app.services.market_game_service import AnalysisBusyError
from app.services.regime_builder import load_regime_history

logger = logging.getLogger(__name__)


def _latest_day(data_dir: Path) -> str | None:
    df = load_regime_history(data_dir)
    if df.is_empty() or "date" not in df.columns:
        return None
    return str(df["date"].max())


def _ensure_plan(service, day: str) -> tuple[bool, str | None]:
    if service is None:
        return False, "博弈服务未加载"
    reports = service.store.list_reports()
    # 当日计划须含纯风险闸门; 旧版计划(无 market_gate)不算, 重新生成一份。
    for item in reports:
        if item.get("as_of") != day:
            continue
        allocation = service.store.get(item["id"]).get("allocation") or {}
        if allocation.get("market_gate") is not None:
            return False, "当日计划已存在"
    risk = None
    if reports:
        latest = service.store.get(reports[0]["id"])
        if isinstance(latest.get("requested_risk"), dict):
            risk = latest["requested_risk"]
    try:
        report = service.generate(None, risk)
    except AnalysisBusyError:
        return False, "已有分析正在生成"
    except Exception as exc:  # a failed plan must not block archiving
        logger.exception("post-close market game plan generation failed")
        return False, f"计划生成失败：{exc}"
    if report.get("as_of") != day:
        logger.info("post-close plan as_of %s differs from regime day %s", report.get("as_of"), day)
    return True, None


def post_close_freeze(app_state) -> dict:
    data_dir = Path(app_state.repo.store.data_dir)
    day = _latest_day(data_dir)
    if day is None:
        return {"date": None, "generated": False, "skipped_reason": "暂无环境历史", "archived": False}
    generated, reason = _ensure_plan(getattr(app_state, "market_game_service", None), day)
    archived = False
    try:
        conclusion = environment_conclusion.build_conclusion(data_dir)
        archived = environment_conclusion_archive.append_version(data_dir, conclusion, "pipeline")
    except Exception:
        logger.exception("post-close environment conclusion archive failed")
    result = {"date": day, "generated": generated, "skipped_reason": reason, "archived": archived}
    logger.info("post-close freeze: %s", result)
    return result
