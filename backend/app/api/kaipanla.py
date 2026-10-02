"""Supplementary datasets share the recap and extension-data storage contracts."""
from __future__ import annotations

from datetime import date as date_cls
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.services import kaipanla, market_emotion
from app.services.kaipanla_catalog import datasets, get_dataset
from app.services.kaipanla_context import read_kaipanla_context

router = APIRouter(prefix="/kaipanla", tags=["market-recap"])


class RefreshRequest(BaseModel):
    date: date_cls | None = None


class DatasetQuery(RefreshRequest):
    parameters: dict[str, str | int | float] = Field(default_factory=dict)
    force: bool = False


def _data_dir(request: Request):
    return request.app.state.repo.store.data_dir


@router.get("/catalog")
def catalog():
    return {
        "source": "开盘啦",
        "datasets": [{
            "id": spec.id, "label": spec.label, "category": spec.category,
            "required_params": spec.required_params, "historical": spec.historical,
            "schedule_minutes": spec.schedule_minutes,
            "persistable": not spec.required_params,
        } for spec in datasets()],
        "excluded": ["板块竞价", "板块内股票竞价", "最强风口", "竞价列表(认证要求待确认)"],
    }


@router.get("/context")
def context(request: Request, date: Annotated[date_cls | None, Query()] = None):
    try:
        day = kaipanla.context_date(_data_dir(request), date)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return read_kaipanla_context(_data_dir(request), day)


@router.get("/market-emotion")
async def market_emotion_dashboard(
    date: Annotated[date_cls | None, Query()] = None,
    force: bool = False,
):
    try:
        return await market_emotion.get_market_emotion(date, force=force)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/refresh")
async def refresh(request: Request, body: RefreshRequest):
    try:
        return await kaipanla.refresh_supplement(_data_dir(request), body.date)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/query/{dataset_id}")
async def query_dataset(dataset_id: str, body: DatasetQuery):
    if get_dataset(dataset_id) is None:
        raise HTTPException(404, "未知或需要登录的数据集")
    try:
        return await kaipanla.fetch_dataset(dataset_id, body.date, body.parameters, force=body.force)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "开盘啦数据获取失败,请稍后重试") from exc
