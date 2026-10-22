"""Register the read-only sentiment menu through the supported extension API."""

from __future__ import annotations

from datetime import date as date_cls
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.data_providers import kaipanla
from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.services import market_emotion

EXTENSION_ID = "kaipanla.market-emotion"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

router = APIRouter(prefix="/api/market-recap/kaipanla", tags=["market-emotion"])
_STOCK_DATASETS = frozenset({"ext_kpl_limit_up", "ext_kpl_broken_limits"})


class DatasetQuery(BaseModel):
    date: date_cls | None = None
    parameters: dict[str, str | int | float] = Field(default_factory=dict)
    force: bool = False


@router.get("/market-emotion")
async def market_emotion_dashboard(
    date: Annotated[date_cls | None, Query()] = None,
    force: bool = False,
):
    try:
        return await market_emotion.get_market_emotion(date, force=force)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/query/{dataset_id}")
async def query_dataset(dataset_id: str, body: DatasetQuery):
    if dataset_id not in _STOCK_DATASETS:
        raise HTTPException(404, "未知或不支持的市场情绪股票列表")
    try:
        return await kaipanla.fetch_dataset(
            dataset_id, body.date, body.parameters, force=body.force
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "开盘啦数据获取失败,请稍后重试") from exc


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
