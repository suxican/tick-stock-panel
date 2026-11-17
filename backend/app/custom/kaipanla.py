"""开盘啦盘中情绪/连板 (非官方接口) 的路由与定时采集。默认关闭。"""

import logging
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.market_time import cn_now
from app.services import preferences
from app.services.kaipanla_intraday import (
    KaipanlaUnavailableError,
    capture,
    read_day,
    read_intraday,
    status,
)
from app.services.trading_day import is_trading_day

EXTENSION_ID = "kaipanla.intraday"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION
logger = logging.getLogger(__name__)
_JOB_ID = "kaipanla_capture"
_PREF_KEY = "kaipanla_enabled"


def _enabled() -> bool:
    return bool(preferences.load().get(_PREF_KEY, False))


def _capture(data_dir: Path) -> None:
    if not _enabled():
        return
    now = cn_now()
    if is_trading_day(now) is False:
        return
    try:
        capture(data_dir, now)
    except Exception:  # noqa: BLE001 -- 定时任务不得因上游异常中断调度
        logger.exception("开盘啦定时采集失败")


@asynccontextmanager
async def _lifespan(app):
    scheduler = getattr(app.state, "scheduler", None)
    registered = False
    if scheduler is not None:
        try:
            # 连续竞价留观测、15:05 定版; 21 点一轮只补前一交易日晋级率
            scheduler.add_job(
                _capture, "cron", args=[app.state.repo.store.data_dir],
                day_of_week="mon-fri", hour="9-15,21", minute="*/5", timezone="Asia/Shanghai",
                id=_JOB_ID, replace_existing=True, coalesce=True, max_instances=1, misfire_grace_time=30,
            )
            registered = True
        except Exception:
            logger.exception("开盘啦定时采集未能启动")
    try:
        yield
    finally:
        if registered and scheduler.get_job(_JOB_ID) is not None:
            scheduler.remove_job(_JOB_ID)


router = APIRouter(prefix="/api/kaipanla", tags=["kaipanla"], lifespan=_lifespan)


class EnabledIn(BaseModel):
    enabled: bool


@router.get("/status")
def get_status() -> dict:
    return {"enabled": _enabled(), **status()}


@router.put("/enabled")
def set_enabled(body: EnabledIn) -> dict:
    preferences.save({_PREF_KEY: body.enabled})
    return {"enabled": body.enabled}


@router.get("/intraday")
def intraday(request: Request, date: date | None = None) -> dict:
    today = cn_now().date()
    if date is not None and date > today:
        raise HTTPException(422, "日期不能晚于今天")
    if not _enabled():
        raise HTTPException(409, "开盘啦数据未启用")
    data_dir = request.app.state.repo.store.data_dir
    if date is None or date == today:
        try:
            return read_intraday(data_dir)
        except KaipanlaUnavailableError as exc:
            raise HTTPException(503, str(exc)) from exc
    snap = read_day(data_dir, date)
    if snap is None:
        raise HTTPException(404, "该日没有开盘啦留档")
    return snap


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
