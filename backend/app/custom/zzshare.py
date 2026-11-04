"""ZZShare 特色复盘数据的手动同步入口。"""

from datetime import date

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.ext_data import _refresh_views
from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.services.zzshare_limit_reasons import (
    LimitReasonsSourceError,
    SnapshotConflictError,
    SourceUnavailableError,
    sync_limit_reasons,
)

EXTENSION_ID = "zzshare.research"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION
router = APIRouter(prefix="/api/zzshare", tags=["zzshare"])


class LimitReasonsSyncRequest(BaseModel):
    date: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


@router.post("/limit-reasons/sync")
def sync(request: Request, body: LimitReasonsSyncRequest) -> dict:
    try:
        day = date.fromisoformat(body.date)
    except ValueError as exc:
        raise HTTPException(422, "日期无效，请使用 YYYY-MM-DD") from exc
    try:
        result = sync_limit_reasons(request.app.state.repo.store.data_dir, day)
    except SnapshotConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except SourceUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc
    except LimitReasonsSourceError as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if result["state"] == "synced":
        _refresh_views(request)
    return result


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
