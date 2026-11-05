"""Read-only overview and source drill-down for the published ETF ledger."""

import logging
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.custom.etf_observation_data.read_model import (
    FUND_NAMES,
    ObservationWindow,
    UnknownSessionError,
)
from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "market.etf-observation"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION
router = APIRouter(prefix="/api/etf-observation", tags=["etf-observation"])
logger = logging.getLogger(__name__)


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)


def _window(request: Request, window: int, as_of: date | None):
    if window not in (1, 20, 60):
        raise HTTPException(422, "请选择 1、20 或 60 个交易日")
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise HTTPException(503, "数据仓库尚未就绪")
    try:
        return ObservationWindow(Path(repo.store.data_dir), window, as_of)
    except UnknownSessionError as exc:
        raise HTTPException(404, "所选日期不在已发布观察区间内") from exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("ETF observation release is unreadable", exc_info=True)
        raise HTTPException(503, "ETF 观察数据暂时无法核验") from exc


@router.get("")
def overview(request: Request, window: int = 1, as_of: date | None = None):
    return _window(request, window, as_of).overview()


@router.get("/detail")
def detail(request: Request, symbol: str, window: int = 1, as_of: date | None = None):
    if symbol not in FUND_NAMES:
        raise HTTPException(422, "请选择观察池中的 ETF")
    return _window(request, window, as_of).detail(symbol)
