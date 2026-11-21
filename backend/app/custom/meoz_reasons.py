"""猫爪个股涨停归因的只读入口 (连板梯队展示用)。"""

import logging
from datetime import date

from fastapi import APIRouter, HTTPException, Request

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.services.meoz_hot_rank import HotRankUnavailableError
from app.services.meoz_hot_rank import read as read_hot_rank
from app.services.meoz_limit_reasons import ReasonsUnavailableError, read
from app.services.meoz_news import NewsUnavailableError
from app.services.meoz_news import read as read_news
from app.services.meoz_strongest_vent import StrongestVentUnavailableError
from app.services.meoz_strongest_vent import read as read_strongest_vent
from app.services.meoz_theme_reasons import ThemeReasonsUnavailableError
from app.services.meoz_theme_reasons import read as read_theme_reasons
from app.services.meoz_theme_reasons import read_latest as read_latest_theme_reasons

logger = logging.getLogger(__name__)

EXTENSION_ID = "meoz.limit_reasons"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

router = APIRouter(prefix="/api/meoz", tags=["meoz"])


@router.get("/limit-reasons")
def limit_reasons(request: Request, date: date) -> dict:
    try:
        return read(request.app.state.repo.store.data_dir, date)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ReasonsUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:  # 上游异常转为可读错误, 不泄露内部细节
        raise HTTPException(502, "猫爪归因暂时不可用，请稍后重试") from exc


@router.get("/theme-reasons")
def theme_reasons(request: Request, date: date, latest: bool = False) -> dict:
    """latest=true: 当日无数据 (休市日等) 时回退到最近有数据的交易日, 包内 date 为实际日期。"""
    try:
        data_dir = request.app.state.repo.store.data_dir
        return (read_latest_theme_reasons if latest else read_theme_reasons)(data_dir, date)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ThemeReasonsUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "猫爪板块原因暂时不可用，请稍后重试") from exc


@router.get("/hot-rank")
def hot_rank() -> dict:
    try:
        return read_hot_rank()
    except HotRankUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "猫爪热榜暂时不可用，请稍后重试") from exc


@router.get("/strongest-vent")
def strongest_vent(date: date | None = None) -> dict:
    try:
        return read_strongest_vent(date)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except StrongestVentUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        logger.warning("猫爪最强风口取数失败", exc_info=True)
        raise HTTPException(502, "猫爪最强风口暂时不可用，请稍后重试") from exc


@router.get("/news")
def news(limit: int = 50) -> dict:
    try:
        return read_news(limit)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except NewsUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        logger.warning("猫爪新闻取数失败", exc_info=True)
        raise HTTPException(502, "猫爪新闻暂时不可用，请稍后重试") from exc


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
