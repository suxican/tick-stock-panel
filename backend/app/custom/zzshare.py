"""ZZShare 特色复盘数据的手动同步入口。"""

from datetime import date

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.ext_data import _refresh_views
from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.market_time import cn_today
from app.services.zzshare_limit_reasons import (
    LimitReasonsSourceError,
    SnapshotConflictError,
    SourceUnavailableError,
    sync_limit_reasons,
)
from app.services.zzshare_radar import get_themes, start_themes_sync, stop_themes_sync
from app.services.zzshare_research import (
    PlateType,
    ResearchError,
    ResearchKind,
    ResearchRateLimitError,
    ResearchReader,
    ResearchUnavailableError,
)

EXTENSION_ID = "zzshare.research"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION
router = APIRouter(prefix="/api/zzshare", tags=["zzshare"])
_reader = ResearchReader()
router.add_event_handler("shutdown", stop_themes_sync)


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


@router.get("/research/{kind}")
def research(
    kind: ResearchKind,
    date: date,
    code: str | None = Query(default=None, pattern=r"^[0-9]{6}(\.(SH|SZ|BJ))?$"),
    plate_type: PlateType | None = None,
) -> dict:
    try:
        return _reader.read(kind, date, code, plate_type)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ResearchUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ResearchRateLimitError as exc:
        raise HTTPException(429, str(exc)) from exc
    except ResearchError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/themes")
def themes(request: Request, date: date) -> dict:
    if date > cn_today():
        raise HTTPException(422, "不能查询未来日期")
    return get_themes(request.app.state.repo, date)


@router.post("/themes/sync", status_code=202)
def sync_themes(request: Request, body: LimitReasonsSyncRequest) -> dict:
    try:
        day = date.fromisoformat(body.date)
        if day > cn_today():
            raise ValueError("不能同步未来日期")
        return start_themes_sync(request.app.state.repo, day)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
