"""持仓研究薄接口：服务端快照、版本化计划与受限价格提醒。"""
from __future__ import annotations

import hashlib
import logging
import threading
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.monitor_rules import _sync_engine
from app.market_time import cn_now
from app.services.holding_analysis import analyze_holdings, read_inputs
from app.services.holding_analysis_store import HoldingAnalysisStore, ResearchError
from app.services.holding_interpretation import interpret_report
from app.strategy import monitor_rules

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/holding-analysis", tags=["holding-analysis"])
_write_lock = threading.RLock()


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SourceBody(Body):
    source: Literal["lots", "position_t"] = "lots"
    account_id: str = Field(default="", max_length=100)


class Profile(Body):
    opened_on: date | None = None
    horizon: Literal["short", "swing"] = "short"
    thesis: str = Field(default="", max_length=2000)
    invalidation: str = Field(default="", max_length=2000)
    max_loss_pct: float | None = Field(default=None, gt=0, lt=100)
    deep_loss_pct: float = Field(default=15, gt=0, lt=100)
    new_position_days: int = Field(default=3, strict=True, ge=1, le=30)
    max_holding_days: int = Field(default=5, strict=True, ge=1, le=250)
    primary_sector_id: str = Field(default="", max_length=160)

    @model_validator(mode="after")
    def validate_date(self):
        if self.opened_on and self.opened_on > cn_now().date():
            raise ValueError("建仓日期不能晚于今天")
        return self


class ProfileBody(SourceBody):
    expected_version: int = Field(strict=True, ge=0)
    profile: Profile


class AdoptBody(Body):
    report_id: str = Field(min_length=1, max_length=100)
    row_key: str = Field(min_length=1, max_length=100)
    expected_input_version: str = Field(min_length=1, max_length=128)


class InvalidateBody(Body):
    expected_version: int = Field(strict=True, ge=1)
    status: Literal["invalidated"]


class AlertBody(Body):
    expected_version: int = Field(strict=True, ge=1)
    scenario_key: Literal["bull", "bear"]


def _dependencies(request):
    state = request.app.state
    repo = getattr(state, "repo", None)
    if repo is None:
        raise HTTPException(503, "行情仓库尚未就绪")
    return repo, getattr(state, "position_t_service", None)


def _store(request):
    repo, _ = _dependencies(request)
    return HoldingAnalysisStore(repo.store.data_dir)


def _inputs(request, source, account_id):
    repo, positions = _dependencies(request)
    if source not in {"lots", "position_t"}:
        raise HTTPException(422, "未知的持仓来源")
    if source == "lots" and account_id:
        raise HTTPException(422, "批次参考不属于核对账户")
    if source == "position_t" and positions is None:
        raise HTTPException(503, "持仓账本尚未就绪")
    return _call(read_inputs, repo, positions, source, account_id)


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ResearchError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        logger.warning("持仓研究文件操作失败: %s", type(exc).__name__)
        raise HTTPException(503, "研究资料读写失败，请检查数据目录后重试") from exc


def _keys(inputs):
    return [row["key"] for row in inputs.get("positions", [])]


def _review_reasons(request, store, report, *, current=None):
    inputs, profile_version = current if current is not None else (
        _inputs(request, report["source"], report.get("account_id", "")), None)
    if profile_version is None:
        profile_version = _call(store.profile_version, _keys(inputs))
    reasons = []
    if inputs["input_version"] != report["input_version"]:
        reasons.append("持仓来源已变化，请重新分析")
    if profile_version != report.get("profile_version"):
        reasons.append("研究设置已变化，请重新分析")
    try:
        if datetime.fromisoformat(report["generated_at"]).date() != cn_now().date():
            reasons.append("分析已跨日，请重新分析后采纳或绑定提醒")
    except (KeyError, ValueError, TypeError):
        reasons.append("分析时间无法核验")
    return reasons


def _assert_current(request, store, report):
    reasons = _review_reasons(request, store, report)
    if reasons:
        raise HTTPException(409, "；".join(reasons))


def _refresh_guard(request):
    guard = getattr(request.app.state, "holding_alert_guard", None)
    if guard is not None:
        try:
            guard.refresh()
        except Exception:  # noqa: BLE001 -- saved research remains valid; admission fails closed
            guard.revoke()
            logger.warning("研究资料已保存，提醒有效性检查失败，提醒暂停")


def _disable_alerts(request, plan):
    repo, _ = _dependencies(request)
    for rule_id in plan.get("alert_rule_ids", []):
        rule = monitor_rules.load_one(repo.store.data_dir, rule_id)
        if rule and rule.get("holding_plan_id") == plan["id"]:
            monitor_rules.save_one(repo.store.data_dir, {**rule, "enabled": False})


@router.get("/overview")
def overview(request: Request, source: Literal["lots", "position_t"] = "lots", account_id: str = ""):
    inputs = _inputs(request, source, account_id)
    store = _store(request)
    keys = _keys(inputs)
    current = (inputs, _call(store.profile_version, keys))
    profiles = _call(store.profiles)
    report = _call(store.latest_report, source, account_id)
    if report:
        report["needs_review"] = bool(_review_reasons(request, store, report, current=current))
    plans = _call(store.list_plans, source, account_id)
    for plan in plans:
        # The frozen row remains visible even when its source was removed.
        reasons = _review_reasons(request, store, plan, current=current)
        expires = plan.get("expires_on")
        if not expires or expires < cn_now().date().isoformat():
            reasons.append("计划已到期")
        plan["needs_review"] = bool(reasons)
        plan["review_reasons"] = reasons
    return {**inputs, "profiles": {key: profiles[key] for key in keys if key in profiles},
            "report": report, "plans": plans}


@router.post("/analyze")
def analyze(request: Request, body: SourceBody):
    repo, positions = _dependencies(request)
    inputs = _inputs(request, body.source, body.account_id)
    if not inputs.get("positions"):
        raise HTTPException(422, "当前来源没有可分析的持仓，请先选择账户或登记批次")
    store = _store(request)
    keys = _keys(inputs)
    version = _call(store.profile_version, keys)
    profiles = {key: value["profile"] for key, value in _call(store.profiles).items() if key in keys}
    report = _call(analyze_holdings, repo, getattr(request.app.state, "quote_service", None),
                   getattr(request.app.state, "sector_monitor_service", None), positions,
                   body.source, body.account_id, profiles=profiles)
    with _write_lock:
        current = _inputs(request, body.source, body.account_id)
        if current["input_version"] != inputs["input_version"] or report["input_version"] != inputs["input_version"]:
            raise HTTPException(409, "分析期间持仓发生变化，请重新分析")
        if store.profile_version(keys) != version:
            raise HTTPException(409, "分析期间研究设置发生变化，请重新分析")
        return _call(store.save_report, {**report, "profile_version": version})


@router.put("/profiles/{key}")
def save_profile(key: str, request: Request, body: ProfileBody):
    with _write_lock:
        inputs = _inputs(request, body.source, body.account_id)
        if key not in _keys(inputs):
            raise HTTPException(404, "此持仓已移除，请刷新列表")
        result = _call(_store(request).save_profile, key, body.profile.model_dump(mode="json"), body.expected_version)
        _refresh_guard(request)
        return result


@router.post("/reports/{report_id}/interpret")
async def interpret(report_id: str, request: Request):
    store = _store(request)
    report = _call(store.get_report, report_id)
    _assert_current(request, store, report)
    try:
        result = await interpret_report(report)
    except ResearchError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    # A late model result cannot be presented as explaining a changed holding context.
    _assert_current(request, store, report)
    return {"report_id": report_id, "generated_at": cn_now().isoformat(), "interpretation": result}


@router.post("/plans")
def adopt(request: Request, body: AdoptBody):
    with _write_lock:
        store = _store(request)
        report = _call(store.get_report, body.report_id)
        _assert_current(request, store, report)
        return {"plan": _call(store.adopt_plan, body.report_id, body.row_key, body.expected_input_version)}


@router.patch("/plans/{plan_id}")
def invalidate(plan_id: str, request: Request, body: InvalidateBody):
    with _write_lock:
        store = _store(request)
        plan = _call(store.invalidate_plan, plan_id, body.expected_version)
        # Commit invalidation first; the in-memory guard independently rejects it on refresh.
        _refresh_guard(request)
        _call(_disable_alerts, request, plan)
        _sync_engine(request)
        return {"plan": plan}


@router.post("/plans/{plan_id}/alerts")
def bind_alert(plan_id: str, request: Request, body: AlertBody):
    with _write_lock:
        store = _store(request)
        plan = _call(store.get_plan, plan_id)
        if plan["status"] != "adopted" or plan["version"] != body.expected_version:
            raise HTTPException(409, "计划状态或版本已变化，请刷新")
        _assert_current(request, store, plan)
        if not plan.get("expires_on") or plan["expires_on"] < cn_now().date().isoformat():
            raise HTTPException(409, "计划已到期，请重新分析")
        guard = getattr(request.app.state, "holding_alert_guard", None)
        if guard is None:
            raise HTTPException(503, "计划提醒守卫未就绪，暂不能绑定提醒")
        scenario = next((s for s in plan["row"]["plan"]["scenarios"] if s["key"] == body.scenario_key), None)
        trigger = scenario.get("price_trigger") if scenario else None
        if not trigger or trigger.get("op") not in {"<=", ">="}:
            raise HTTPException(422, "此情景没有可验证的原始价格条件")
        repo, _ = _dependencies(request)
        rule_id = "ha_" + hashlib.sha256(f"{plan_id}:{body.scenario_key}".encode()).hexdigest()[:28]
        old_rule = monitor_rules.load_one(repo.store.data_dir, rule_id)
        if old_rule and old_rule.get("holding_plan_id") != plan_id:
            raise HTTPException(409, "提醒标识冲突")
        rule = monitor_rules.normalize({
            "id": rule_id, "name": f"持仓研究 · {plan['row'].get('name') or plan['row']['symbol']} · {scenario['title']}",
            "type": "price", "asset_type": plan["row"]["asset_type"], "scope": "symbols",
            "symbols": [plan["row"]["symbol"]], "enabled": False,
            "conditions": [{"field": "raw_close", **trigger}], "logic": "and",
            "cooldown_seconds": 3600, "severity": "warn", "webhook_channels": [],
            "holding_plan_id": plan_id,
            "message": f"持仓研究价格条件满足：{scenario['title']}。仅价格提醒，请复核板块、持仓和完整计划；不表示委托或成交。",
        })
        _call(monitor_rules.validate, rule)
        # Persist pending before touching any rule: even a failed rebind stays paused after restart.
        plan = _call(store.set_alert_rule, plan_id, body.expected_version, rule_id)
        guard.revoke()
        try:
            _call(monitor_rules.save_one, repo.store.data_dir, rule)
            _refresh_guard(request)
            _call(monitor_rules.save_one, repo.store.data_dir, {**rule, "enabled": True})
            _sync_engine(request)
            plan = _call(store.activate_alert_rule, plan_id, plan["version"], rule_id)
            _refresh_guard(request)
        except Exception as exc:
            guard.revoke()
            try:
                monitor_rules.save_one(repo.store.data_dir, rule)
                _sync_engine(request)
            except Exception:  # noqa: BLE001 -- engine guard remains revoked even if storage is unavailable
                logger.warning("研究提醒同步失败，已撤销准入，需重新绑定")
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(503, "提醒同步未完成，已暂停该提醒；请重新读取计划后绑定") from exc
        return {"plan": plan, "rule_id": rule_id}
