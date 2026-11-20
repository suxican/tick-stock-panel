"""ZZShare 题材雷达与复盘数据入口。"""

import logging
import re
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.ext_data import _refresh_views
from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.market_time import CN_TZ, cn_now, cn_today, in_continuous_session
from app.services import preferences
from app.services.dimension_intraday import member_intraday
from app.services.trading_day import is_trading_day
from app.services.zzshare_home import read_home
from app.services.zzshare_limit_reasons import (
    LimitReasonsSourceError,
    SnapshotConflictError,
    SourceUnavailableError,
    sync_limit_reasons,
)
from app.services.zzshare_radar import (
    MemberCacheError,
    get_themes,
    member_snapshot,
    read_cached_members,
    start_themes_sync,
    stop_themes_sync,
)
from app.services.zzshare_research import (
    PlateType,
    ResearchError,
    ResearchKind,
    ResearchRateLimitError,
    ResearchUnavailableError,
    shared_reader,
)

EXTENSION_ID = "zzshare.research"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION
logger = logging.getLogger(__name__)
_CAPTURE_JOB = "zzshare_home_snapshot"
_REASONS_JOB = "zzshare_limit_reasons_sync"
_DEFAULT_REASONS_TIME = "17:30"
_TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"
_reader = shared_reader


def _capture_home(data_dir: Path) -> None:
    now = cn_now()
    # 午休与休市停采。15:05 另取当日收盘榜。
    if not in_continuous_session(now) and (now.hour, now.minute) != (15, 5):
        return
    if is_trading_day(now) is not False:
        read_home(data_dir, now.date())


def get_reasons_schedule() -> dict:
    data = preferences.load()
    enabled = data.get("zzshare_reasons_schedule_enabled", False)
    hhmm = data.get("zzshare_reasons_schedule_time", _DEFAULT_REASONS_TIME)
    if not isinstance(hhmm, str) or not re.match(_TIME_PATTERN, hhmm):
        hhmm = _DEFAULT_REASONS_TIME
    last = data.get("zzshare_reasons_last_run")
    return {
        "enabled": enabled if isinstance(enabled, bool) else False,
        "time": hhmm,
        "last_run": last if isinstance(last, dict) else None,
    }


def _run_reasons_sync(app) -> None:
    """定时任务: 交易日到点同步当日涨停归因, 结果记入偏好供前端展示。"""
    now = cn_now()
    if is_trading_day(now) is False:
        return
    record: dict = {"at": now.isoformat(timespec="seconds"), "date": now.date().isoformat()}
    try:
        result = sync_limit_reasons(app.state.repo.store.data_dir, now.date())
        record["state"] = result["state"]
        if result["state"] == "synced":
            _refresh_views(SimpleNamespace(app=app))
    except Exception as exc:  # 定时任务不能抛出; 失败原因留给前端展示
        logger.warning("涨停归因定时同步失败: %s", exc)
        record["state"] = "error"
        record["message"] = str(exc)
    preferences.save({"zzshare_reasons_last_run": record})


def apply_reasons_schedule(app) -> str | None:
    """按当前偏好重新注册 (或移除) 涨停归因定时任务, 返回下次执行时间。"""
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is None:
        return None
    if scheduler.get_job(_REASONS_JOB) is not None:
        scheduler.remove_job(_REASONS_JOB)
    config = get_reasons_schedule()
    if not config["enabled"]:
        return None
    hour, minute = config["time"].split(":")
    job = scheduler.add_job(
        _run_reasons_sync, "cron", args=[app],
        day_of_week="mon-fri", hour=int(hour), minute=int(minute), timezone="Asia/Shanghai",
        id=_REASONS_JOB, replace_existing=True, coalesce=True, max_instances=1, misfire_grace_time=600,
    )
    nxt = getattr(job, "next_run_time", None)
    return nxt.isoformat() if nxt else None


@asynccontextmanager
async def _lifespan(app):
    scheduler = getattr(app.state, "scheduler", None)
    registered = False
    if scheduler is not None:
        try:
            scheduler.add_job(
                _capture_home, "cron", args=[app.state.repo.store.data_dir],
                day_of_week="mon-fri", hour="9-15", minute="*/5", timezone="Asia/Shanghai",
                id=_CAPTURE_JOB, replace_existing=True, coalesce=True, max_instances=1, misfire_grace_time=30,
            )
            registered = True
        except Exception:
            logger.exception("题材快照定时采集未能启动")
        try:
            apply_reasons_schedule(app)
        except Exception:
            logger.exception("涨停归因定时同步未能启动")
    try:
        yield
    finally:
        if registered and scheduler.get_job(_CAPTURE_JOB) is not None:
            scheduler.remove_job(_CAPTURE_JOB)
        if scheduler is not None and scheduler.get_job(_REASONS_JOB) is not None:
            scheduler.remove_job(_REASONS_JOB)
        stop_themes_sync()


router = APIRouter(prefix="/api/zzshare", tags=["zzshare"], lifespan=_lifespan)


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


class ReasonsScheduleRequest(BaseModel):
    enabled: bool
    time: str = Field(pattern=_TIME_PATTERN)


def _reasons_schedule_payload(request: Request) -> dict:
    scheduler = getattr(request.app.state, "scheduler", None)
    job = scheduler.get_job(_REASONS_JOB) if scheduler is not None else None
    nxt = getattr(job, "next_run_time", None) if job is not None else None
    return {**get_reasons_schedule(), "next_run": nxt.isoformat() if nxt else None}


@router.get("/limit-reasons/schedule")
def reasons_schedule(request: Request) -> dict:
    return _reasons_schedule_payload(request)


@router.put("/limit-reasons/schedule")
def set_reasons_schedule(request: Request, body: ReasonsScheduleRequest) -> dict:
    preferences.save({
        "zzshare_reasons_schedule_enabled": body.enabled,
        "zzshare_reasons_schedule_time": body.time,
    })
    try:
        apply_reasons_schedule(request.app)
    except Exception as exc:
        logger.exception("涨停归因定时任务注册失败")
        raise HTTPException(500, "定时任务注册失败") from exc
    return _reasons_schedule_payload(request)


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


@router.get("/home-topics")
def home_topics(request: Request, date: date, as_of: datetime | None = None) -> dict:
    now = cn_now()
    if date > now.date():
        raise HTTPException(422, "不能查询未来交易日")
    if as_of is not None:
        if as_of.tzinfo is None:
            raise HTTPException(422, "回看时刻必须包含时区")
        as_of = as_of.astimezone(CN_TZ)
        if as_of.date() != date or as_of > now:
            raise HTTPException(422, "请选择交易日内已发生的时刻")
    return read_home(request.app.state.repo.store.data_dir, date, as_of=as_of)


def _archived_members(request: Request, day: date, plate_code: str):
    try:
        return read_cached_members(request.app.state.repo, day, plate_code)
    except FileNotFoundError as exc:
        raise HTTPException(404, "本地暂无该日题材成员快照，请先计算当日题材排名。") from exc
    except MemberCacheError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/themes/{plate_code}/members")
def theme_members(request: Request, plate_code: str, date: date) -> dict:
    members = _archived_members(request, date, plate_code)
    return member_snapshot(request.app.state.repo, date, plate_code, members)


@router.get("/themes/{plate_code}/intraday")
def theme_intraday(request: Request, plate_code: str, date: date) -> dict:
    members = _archived_members(request, date, plate_code)
    return member_intraday(request.app.state.repo.store.data_dir, date, list(members))


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
