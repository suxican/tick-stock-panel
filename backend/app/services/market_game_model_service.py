"""Explicit research refresh; no broker access or changes to a frozen plan."""
# ruff: noqa: RUF001
from __future__ import annotations

import json
import threading
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.market_time import CN_TZ, cn_now
from app.services.market_game_adaptation import analyze_adaptation
from app.services.market_game_capital import build_capital_behavior
from app.services.market_game_ecology_models import FeedbackObservation
from app.services.market_game_execution import evaluate_executions
from app.services.market_game_execution_models import AdaptationResult, ExecutionEvaluation
from app.services.market_game_execution_service import build_execution_evidence
from app.services.market_game_feedback import observe_feedback
from app.services.market_game_models import GameReport

LIMITATIONS = [
    "新增竞争与反馈规则仅用于研究观察；不授权入场，不提高原计划仓位。",
    "模拟成交与候选后续表现分别保存，真实账户资金及委托成交仍未验证。",
    "适应性只读取已归档且在评估时点已成熟的模拟结果；始终处于影子观察。",
    "历史统计最多读取最近400份评估文件、200份报告；相同报告刷新不增加策略样本。",
]


class ModelEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    report_id: str
    evaluated_at: str | None = None
    feedback: list[FeedbackObservation] = Field(default_factory=list, max_length=60)
    execution: ExecutionEvaluation | None = None
    adaptation: AdaptationResult | None = None
    limitations: list[str] = Field(default_factory=lambda: list(LIMITATIONS))


class ModelBusyError(RuntimeError):
    pass


def _time(value: datetime | None = None) -> datetime:
    value = value or cn_now()
    if value.tzinfo is None:
        raise ValueError("模型评估时间须包含时区")
    return value.astimezone(CN_TZ)


def _feedback(records: list[dict]) -> list[dict]:
    """Repeated reads of one source minute are not independent evidence."""
    selected = {}
    for item in records:
        checked = FeedbackObservation.model_validate(item).model_dump(mode="json")
        identity = json.dumps({key: checked[key] for key in ("hypothesis_id", "observed_at", "rows")},
                              ensure_ascii=False, sort_keys=True)
        selected.setdefault(identity, checked)
    return sorted(selected.values(), key=lambda item: item["observed_at"] or "", reverse=True)[:60]


class MarketGameModelService:
    def __init__(self, repo, store):
        self.repo, self.store = repo, store
        self._guard = threading.Lock()
        self._evaluating = False

    def get(self, report_id: str) -> dict:
        """GET never fetches sources, recomputes labels or modifies archives."""
        self.store.get(report_id)
        saved = self.store.model_evaluations(report_id, limit=1)
        value = saved[0] if saved else {"report_id": report_id}
        return ModelEnvelope.model_validate(value).model_dump(mode="json")

    def evaluate(self, report_id: str, *, now: datetime | None = None) -> dict:
        now = _time(now)
        with self._guard:
            if self._evaluating:
                raise ModelBusyError("已有模型评估正在运行")
            self._evaluating = True
        try:
            report = GameReport.model_validate(self.store.get(report_id)).model_dump(mode="json")
            if datetime.fromisoformat(report["cutoff"]) > now:
                raise ValueError("不能在原计划截止之前评估")
            snapshot = self.store.snapshot(report_id)
            prior = [item for item in self.store.model_evaluations(report_id)
                     if datetime.fromisoformat(item["evaluated_at"]) <= now]
            feedback = [item for record in prior for item in record.get("feedback", [])]
            capital_error = None
            try:
                capital = self.store.execution_capital_observations(report_id)
            except (ValueError, OSError):
                capital = []
                capital_error = "资金观察归档缺损或超过400条，无法完整重放此前收紧约束，本次不模拟成交"
            feedback.extend(record["feedback"] for record in capital if record.get("feedback")
                            and datetime.fromisoformat(record["created_at"]) <= now)
            if report.get("feedback_hypothesis"):
                behavior = build_capital_behavior(self.repo, report, snapshot, now=now)
                feedback.insert(0, observe_feedback(report, behavior, now=now))
            inputs = ({"sessions": {}, "error": capital_error} if capital_error else
                      build_execution_evidence(self.repo, report, snapshot, now=now, observations=capital))
            execution = evaluate_executions(self.repo, report, snapshot, now=now, evidence=inputs)
            limitations = list(LIMITATIONS)
            try:
                history = self.store.execution_history(before=now)
            except (ValueError, OSError):
                history = []
                limitations.append("历史执行评估归档校验失败，本次适应性统计仅使用当前报告；未覆盖历史文件。")
            adaptation = analyze_adaptation([*history, execution], now=now)
            value = ModelEnvelope.model_validate({
                "report_id": report_id, "evaluated_at": now.isoformat(timespec="seconds"),
                "feedback": _feedback(feedback), "execution": execution, "adaptation": adaptation,
                "limitations": limitations,
            }).model_dump(mode="json")
            self.store.save_model_evaluation(report_id, value, execution_inputs=inputs)
            return value
        finally:
            with self._guard:
                self._evaluating = False
