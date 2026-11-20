"""交易所参考数据的定时同步与回填入口。生效源不可用时定时任务静默跳过。"""

import logging
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.market_time import cn_now
from app.services.market_reference import (
    DATASETS,
    ReferenceUnavailableError,
    enable_reader,
    start_backfill,
    status,
    stop_backfill,
    sync_day,
)
from app.services.trading_day import is_trading_day

EXTENSION_ID = "market.reference"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION
logger = logging.getLogger(__name__)
_JOB_IDS = ("market_reference_preopen", "market_reference_close", "market_reference_auction")


def _sync_today(data_dir: Path, datasets: tuple[str, ...]) -> None:
    now = cn_now()
    if is_trading_day(now) is not True:  # 日历未知时不同步, 避免把休市日记成交易日
        return
    for dataset in datasets:
        try:
            sync_day(data_dir, dataset, now.date())
        except ReferenceUnavailableError:
            logger.debug("%s 数据源不可用, 跳过同步", dataset)
        except Exception:  # 定时任务不得因上游异常中断调度
            logger.exception("%s 当日同步失败", dataset)


@asynccontextmanager
async def _lifespan(app):
    scheduler = getattr(app.state, "scheduler", None)
    registered: list[str] = []
    # enriched 涨跌停计算从此读取按日交易所价与 ST 名单; 无落盘时自动回退本地推算
    enable_reader(app.state.repo.store.data_dir)
    if scheduler is not None:
        data_dir = app.state.repo.store.data_dir
        jobs = (
            # 盘前: 当日涨跌停价、ST 与停牌名单 (盘中涨跌停判定、持仓做T与持仓分析要用);
            # 09:20 补同步首轮失败的数据集 (已有的跳过)
            (_JOB_IDS[0], ("price_limits", "st_status", "suspensions"), {"hour": 9, "minute": "5,20"}),
            # 盘后: 涨停池定版; 20:45 补同步首轮失败的数据集 (已有的不改写)
            (_JOB_IDS[1], tuple(DATASETS), {"hour": "15,20", "minute": 45}),
            # 集合竞价 09:25 定版后同步 (竞价情绪当日即可展示); 失败由 15:45/20:45 补同步
            (_JOB_IDS[2], ("auction",), {"hour": 9, "minute": 27}),
        )
        for job_id, datasets, when in jobs:
            try:
                scheduler.add_job(
                    _sync_today, "cron", args=[data_dir, datasets], day_of_week="mon-fri",
                    timezone="Asia/Shanghai", id=job_id, replace_existing=True, coalesce=True,
                    max_instances=1, misfire_grace_time=600, **when,
                )
                registered.append(job_id)
            except Exception:
                logger.exception("交易所参考数据定时同步未能启动")
    try:
        yield
    finally:
        for job_id in registered:
            if scheduler.get_job(job_id) is not None:
                scheduler.remove_job(job_id)
        stop_backfill()
        enable_reader(None)


router = APIRouter(prefix="/api/market-reference", tags=["market-reference"], lifespan=_lifespan)


class BackfillIn(BaseModel):
    dataset: str
    start: date | None = None
    end: date | None = None


@router.get("/status")
def get_status(request: Request) -> dict:
    return status(request.app.state.repo.store.data_dir)


@router.post("/backfill")
def backfill(request: Request, body: BackfillIn) -> dict:
    if body.dataset not in DATASETS:
        raise HTTPException(422, f"未知数据集: {body.dataset}")
    try:
        return start_backfill(request.app.state.repo.store.data_dir, body.dataset, body.start, body.end)
    except ReferenceUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/backfill/stop")
def backfill_stop() -> dict | None:
    return stop_backfill()


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
