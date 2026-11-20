"""猫爪个股涨停归因的只读入口 (连板梯队展示用)。"""

from datetime import date

from fastapi import APIRouter, HTTPException, Request

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.services.meoz_hot_rank import HotRankUnavailableError
from app.services.meoz_hot_rank import read as read_hot_rank
from app.services.meoz_limit_reasons import ReasonsUnavailableError, read
from app.services.meoz_theme_reasons import ThemeReasonsUnavailableError
from app.services.meoz_theme_reasons import read as read_theme_reasons

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
def theme_reasons(request: Request, date: date) -> dict:
    try:
        return read_theme_reasons(request.app.state.repo.store.data_dir, date)
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


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
