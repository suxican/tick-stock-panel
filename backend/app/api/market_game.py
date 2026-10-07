"""Research plan API. Source assembly and computations stay in services."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, HTTPException, Request

from app.services.market_game_models import AnalyzeRequest
from app.services.market_game_service import AnalysisBusyError, MarketGameService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.market_game_service = None
    try:
        repo = getattr(app.state, "repo", None)
        if repo is not None:
            app.state.market_game_service = MarketGameService(repo, data_dir=repo.store.data_dir)
    except Exception:
        logger.exception("超短线博弈服务初始化失败, 其他功能继续运行")
    try:
        yield
    finally:
        app.state.market_game_service = None


router = APIRouter(prefix="/api/market-game", tags=["market-game"], lifespan=_lifespan)


def _service(request: Request):
    service = getattr(request.app.state, "market_game_service", None)
    if service is None:
        raise HTTPException(503, "博弈分析服务未就绪, 请检查后端启动状态")
    return service


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, FileNotFoundError):
        return HTTPException(404, "博弈报告不存在")
    if isinstance(exc, AnalysisBusyError):
        return HTTPException(409, "已有分析正在生成, 请稍后重试")
    if isinstance(exc, ValueError):
        # Errors from data/JSON validators may contain machine paths or rows.
        return HTTPException(400, "日期、数据或报告无效, 请检查数据覆盖后重试")
    logger.error("博弈分析请求失败", exc_info=exc)
    return HTTPException(503, "博弈分析暂时不可用, 已保存的报告不受影响")


def _call(request: Request, method: str, *args):
    service = _service(request)
    try:
        return getattr(service, method)(*args)
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/analyze")
def analyze(request: Request, body: AnalyzeRequest):
    return _call(request, "generate", body.as_of, body.risk.model_dump())


@router.get("/reports")
def reports(request: Request):
    return {"reports": _call(request, "list_reports")}


@router.get("/reports/{report_id}")
def report(request: Request, report_id: str):
    return _call(request, "get_report", report_id)


@router.get("/reports/{report_id}/evaluation")
def evaluation(request: Request, report_id: str):
    return _call(request, "evaluate", report_id)


@router.post("/reports/{report_id}/explain")
async def explain(request: Request, report_id: str):
    service = _service(request)
    try:
        return await service.explain(report_id)
    except Exception as exc:
        raise _error(exc) from exc


@router.get("/reports/{report_id}/capital")
def capital(request: Request, report_id: str):
    return _call(request, "capital", report_id)


@router.post("/reports/{report_id}/capital")
def refresh_capital(request: Request, report_id: str):
    return _call(request, "refresh_capital", report_id)


@router.get("/reports/{report_id}/model")
def model(request: Request, report_id: str):
    return _call(request, "model", report_id)


@router.post("/reports/{report_id}/evaluate-model")
def evaluate_model(request: Request, report_id: str):
    return _call(request, "evaluate_model", report_id)
