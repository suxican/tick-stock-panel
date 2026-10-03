"""首板工作台 HTTP 契约; 领域计算和持久化由服务处理。"""
from datetime import date
from typing import Self

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.first_board import FirstBoardConfig
from app.strategy.first_board import FirstBoardRules

router = APIRouter(prefix="/api/first-board", tags=["first-board"])


def _service(request: Request):
    service = getattr(request.app.state, "first_board_service", None)
    if service is None:
        raise HTTPException(503, "首板服务未就绪, 请检查启动状态")
    return service


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date
    end: date
    rules: FirstBoardRules | None = None

    @model_validator(mode="after")
    def validate_dates(self) -> Self:
        if self.start > self.end:
            raise ValueError("研究开始日期不能晚于结束日期")
        return self


class PaperOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    event_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9]+$")
    side: str = Field(pattern=r"^(buy|sell)$")
    amount: float | None = Field(default=None, gt=0, strict=True)
    qty: int | None = Field(default=None, gt=0, multiple_of=100, strict=True)


@router.get("/config")
def config(request: Request):
    try:
        return _service(request).get_config()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.put("/config")
def save_config(request: Request, body: FirstBoardConfig):
    try:
        return _service(request).save_config(body.model_dump(mode="json"))
    except ValueError as exc:
        raise HTTPException(409 if "版本已变化" in str(exc) else 400, str(exc)) from exc


@router.get("/versions")
def versions(request: Request):
    try:
        return {"versions": _service(request).versions()}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/snapshot")
def snapshot(request: Request):
    service = _service(request)
    service.request_refresh()
    return service.snapshot()


@router.post("/refresh")
def refresh(request: Request):
    service = _service(request)
    service.request_refresh(force=True)
    return service.snapshot()


@router.get("/events")
def events(request: Request, day: date | None = None):
    from app.market_time import cn_today

    day = day or cn_today()
    try:
        records = _service(request).events(day)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"events": records[:5000], "day": str(day), "total": len(records)}


@router.post("/research")
def research(request: Request, body: ResearchRequest):
    try:
        return _service(request).research(start=body.start, end=body.end, rules=body.rules)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/compare")
def compare(request: Request, body: ResearchRequest):
    try:
        return _service(request).research(start=body.start, end=body.end, rules=body.rules, compare=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/research-runs")
def research_runs(request: Request):
    try:
        return {"runs": _service(request).research_runs()}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/paper-order")
def paper_order(request: Request, body: PaperOrderRequest):
    if body.qty is not None and body.amount is not None:
        raise HTTPException(400, "数量与金额只能提供一个")
    try:
        return {"order": _service(request).place_order(**body.model_dump())}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
