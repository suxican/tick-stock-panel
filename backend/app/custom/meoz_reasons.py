"""猫爪个股涨停归因的只读入口 (连板梯队展示用)。"""

from datetime import date

from fastapi import APIRouter, HTTPException, Request

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.services.meoz_limit_reasons import ReasonsUnavailableError, read

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


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
