"""回春模式 HTTP 契约, 筛选与收益计算由后台服务完成。"""
import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.services.huichun_mode import (
    HuichunModeService,
    HuichunRules,
    HuichunScanRequest,
    HuichunTrackRequest,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # include_router 合并生命周期: 此时核心仓库已就绪, 退出时先停止本服务。
    service = None
    app.state.huichun_mode_service = None
    try:
        repo = getattr(app.state, "repo", None)
        store = getattr(app.state, "datastore", None)
        if repo is not None and store is not None:
            service = await run_in_threadpool(
                HuichunModeService, repo, data_dir=store.data_dir,
            )
            app.state.huichun_mode_service = service
    except Exception:
        logger.exception("回春模式初始化失败, 其他功能继续运行")
    try:
        yield
    finally:
        app.state.huichun_mode_service = None
        if service is not None:
            await run_in_threadpool(service.close)


router = APIRouter(prefix="/api/huichun", tags=["huichun"], lifespan=_lifespan)


class ConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    rules: HuichunRules
    expected_revision: int = Field(ge=0, strict=True)


def _call(request: Request, method: str, *args, **kwargs):
    service = getattr(request.app.state, "huichun_mode_service", None)
    if service is None:
        raise HTTPException(503, "回春服务未就绪, 请检查后端启动状态")
    try:
        return getattr(service, method)(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(409 if "版本已变化" in str(exc) else 400, str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        logger.exception("回春服务请求失败: %s", method)
        raise HTTPException(503, "回春数据暂时不可用, 请检查后端日志后重试") from exc


@router.get("/config")
def config(request: Request):
    return _call(request, "get_config")


@router.put("/config")
def save_config(request: Request, body: ConfigRequest):
    return _call(
        request, "save_config", body.rules.model_dump(), expected_revision=body.expected_revision,
    )


@router.get("/snapshot")
def snapshot(request: Request):
    return _call(request, "get_snapshot")


@router.post("/scan", status_code=202)
def scan(request: Request, body: HuichunScanRequest):
    return _call(request, "start_scan", start_date=body.start_date, end_date=body.end_date)


@router.get("/tracking")
def tracking(request: Request):
    return _call(request, "get_tracking")


@router.post("/tracking")
def add_tracking(request: Request, body: HuichunTrackRequest):
    return _call(request, "add_tracking", scan_id=body.scan_id, candidate_ids=body.candidate_ids)


@router.post("/tracking/refresh", status_code=202)
def refresh_tracking(request: Request):
    return _call(request, "start_tracking_refresh")


@router.delete("/tracking/{record_id}")
def remove_tracking(request: Request, record_id: str):
    return _call(request, "remove_tracking", record_id)
