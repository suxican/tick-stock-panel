"""资讯分析页的数据入口。"""

import datetime as dt
from contextlib import asynccontextmanager
from datetime import date

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.market_time import cn_today
from app.services import watchlist
from app.services.news_announcements import get_risks, start_sync, stop_sync
from app.services.news_catalysts import get_catalysts

EXTENSION_ID = "official.news"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


@asynccontextmanager
async def _lifespan(app):
    try:
        yield
    finally:
        stop_sync()


router = APIRouter(prefix="/api/news", tags=["news"], lifespan=_lifespan)


def _watch_symbols() -> list[str]:
    return [row["symbol"] for row in watchlist.list_symbols()]


def _not_future(day: date | None) -> None:
    if day is not None and day > cn_today():
        raise HTTPException(422, "不能查询未来日期")


@router.get("/catalysts")
def catalysts(request: Request, date: date | None = None) -> dict:
    """某交易日的催化分组与盘面核对; 缺省取北京今天, 非交易日回退到最近交易日。"""
    _not_future(date)
    return get_catalysts(request.app.state.repo, date)


@router.get("/announcements")
def announcements(request: Request, date: date) -> dict:
    """自选股在回看窗口内的风险公告; 只读本地快照, 拉取见 /announcements/sync。"""
    _not_future(date)
    return get_risks(request.app.state.repo.store.data_dir, date, _watch_symbols())


class SyncRequest(BaseModel):
    date: dt.date


@router.post("/announcements/sync", status_code=202)
def sync_announcements(request: Request, body: SyncRequest) -> dict:
    _not_future(body.date)
    return start_sync(request.app.state.repo.store.data_dir, body.date, _watch_symbols())


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
