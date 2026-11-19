"""猫爪当前快照盘后留档的调度与入口。猫爪插件未配置时定时任务静默跳过。"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.market_time import cn_now
from app.services.meoz_archive import ArchiveUnavailableError, archive_today, status
from app.services.meoz_limit_reasons import read as read_reasons
from app.services.trading_day import is_trading_day

EXTENSION_ID = "meoz.archive"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION
logger = logging.getLogger(__name__)
_JOB_ID = "meoz_snapshot_archive"


def _run(data_dir: Path) -> None:
    # 只在确认的交易日留档: 未知日历时不存, 避免把休市日的当前快照记成交易日数据
    if is_trading_day(cn_now()) is not True:
        return
    try:
        archive_today(data_dir)
    except ArchiveUnavailableError:
        logger.debug("猫爪未就绪, 跳过盘后留档")
        return
    except Exception:  # 定时任务不得因上游异常中断调度
        logger.exception("猫爪盘后留档失败")
    try:  # 个股涨停归因随盘后留档定版 (已存在则不改写)
        read_reasons(data_dir, cn_now().date())
    except Exception:  # 与快照留档互不影响
        logger.exception("猫爪涨停归因留档失败")


@asynccontextmanager
async def _lifespan(app):
    scheduler = getattr(app.state, "scheduler", None)
    registered = False
    if scheduler is not None:
        try:
            # 15:40 首次留档, 20:40 补存首轮失败或上游迟到的数据集 (已存的不改写)
            scheduler.add_job(
                _run, "cron", args=[app.state.repo.store.data_dir],
                day_of_week="mon-fri", hour="15,20", minute=40, timezone="Asia/Shanghai",
                id=_JOB_ID, replace_existing=True, coalesce=True, max_instances=1, misfire_grace_time=600,
            )
            registered = True
        except Exception:
            logger.exception("猫爪盘后留档未能启动")
    try:
        yield
    finally:
        if registered and scheduler.get_job(_JOB_ID) is not None:
            scheduler.remove_job(_JOB_ID)


router = APIRouter(prefix="/api/meoz-archive", tags=["meoz"], lifespan=_lifespan)


@router.get("/status")
def get_status(request: Request) -> dict:
    return status(request.app.state.repo.store.data_dir)


@router.post("/run")
def run(request: Request) -> dict:
    if is_trading_day(cn_now()) is not True:
        raise HTTPException(409, "今天不是已确认的交易日，不留档")
    try:
        return archive_today(request.app.state.repo.store.data_dir)
    except ArchiveUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
