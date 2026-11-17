"""Read-only overview and source drill-down for the published ETF ledger.

Page requests only read. Scheduled jobs append newly published sessions; the
data page can also ask for the same append explicitly.
"""

import logging
import threading
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request

from app.custom.etf_observation_data import turnover
from app.custom.etf_observation_data.daily import (
    append_new_sessions,
    last_published,
    overdue,
    pending_sessions,
)
from app.custom.etf_observation_data.read_model import (
    FUND_NAMES,
    ObservationWindow,
    UnknownSessionError,
    _disclosures,
)
from app.enriched_generation import EnrichedGenerationUnavailableError
from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.market_time import cn_now
from app.services.daily_health import DailyIntegrityError

EXTENSION_ID = "market.etf-observation"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION
logger = logging.getLogger(__name__)
_APPEND_JOB = "etf_observation_daily_append"
_CATCH_UP_JOB = "etf_observation_startup_catch_up"
# Lets startup reads settle first; a quick dev reload cancels it before it fires.
_CATCH_UP_DELAY = timedelta(seconds=60)
_APPEND_LOCK = threading.Lock()


def _locked_append(data_dir: Path) -> list[str] | None:
    """Days added, or None while another run holds the release."""
    # The cron, startup catch-up and data page button are separate callers, so
    # max_instances cannot keep them apart.
    if not _APPEND_LOCK.acquire(blocking=False):
        return None
    try:
        return append_new_sessions(Path(data_dir), cn_now().date())
    finally:
        _APPEND_LOCK.release()


def _append(data_dir: Path) -> None:
    try:
        added = _locked_append(data_dir)
        if added is None:
            logger.info("ETF ledger append already running; skipping")
        elif added:
            logger.info("ETF ledger appended %s", ", ".join(added))
    except Exception:
        # Source outages or a conflicting revision leave the release untouched; retry next run.
        logger.exception("ETF ledger daily append failed")


def _catch_up(data_dir: Path) -> None:
    # A cron slot passed while the backend was down or asleep is never replayed,
    # which left the ledger days behind for users who rarely run at 8:30/21:30.
    try:
        due = overdue(Path(data_dir), cn_now().date())
    except Exception:
        logger.exception("ETF ledger catch-up check failed")
        return
    if due:
        _append(data_dir)


@asynccontextmanager
async def _lifespan(app):
    scheduler = getattr(app.state, "scheduler", None)
    registered = []
    if scheduler is not None:
        data_dir = app.state.repo.store.data_dir
        try:
            # Evening for same-day units and NAVs; morning catches regulator reports published overnight.
            scheduler.add_job(
                _append, "cron", args=[data_dir],
                hour="8,21", minute=30, timezone="Asia/Shanghai",
                id=_APPEND_JOB, replace_existing=True, coalesce=True, max_instances=1, misfire_grace_time=3600,
            )
            registered.append(_APPEND_JOB)
            scheduler.add_job(
                _catch_up, "date", args=[data_dir], run_date=cn_now() + _CATCH_UP_DELAY,
                id=_CATCH_UP_JOB, replace_existing=True, misfire_grace_time=3600,
            )
            registered.append(_CATCH_UP_JOB)
        except Exception:
            logger.exception("ETF 份额台账定时追加未能启动")
    try:
        yield
    finally:
        for job_id in registered:
            if scheduler.get_job(job_id) is not None:
                scheduler.remove_job(job_id)


router = APIRouter(prefix="/api/etf-observation", tags=["etf-observation"], lifespan=_lifespan)


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


@router.get("/turnover")
def turnover_view(request: Request):
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise HTTPException(503, "数据仓库尚未就绪")
    try:
        shares = turnover.share_series(ObservationWindow(Path(repo.store.data_dir), 60, None))
    except (OSError, ValueError, KeyError, TypeError):
        # The share ledger is independent evidence; its absence must not hide turnover.
        logger.warning("ETF share ledger unavailable for turnover view", exc_info=True)
        shares = {"series": [], "units_as_of": None, "ledger": None}
    try:
        disclosures = _disclosures()
        result = turnover.snapshot(repo, cn_now(), shares["ledger"], turnover.huijin_units(disclosures))
    except (DailyIntegrityError, EnrichedGenerationUnavailableError) as exc:
        raise HTTPException(503, "ETF 日 K 数据暂不可用，请先完成 ETF 日 K 同步") from exc
    return dict(
        result,
        thresholds={"strong": turnover.STRONG, "mild": turnover.MILD},
        shares={"series": shares["series"], "units_as_of": shares["units_as_of"]},
        huijin_period_end=next((f["period_end"] for f in disclosures["funds"]), None),
    )


@router.get("/detail")
def detail(request: Request, symbol: str, window: int = 1, as_of: date | None = None):
    if symbol not in FUND_NAMES:
        raise HTTPException(422, "请选择观察池中的 ETF")
    return _window(request, window, as_of).detail(symbol)


@router.post("/ledger/append")
def append_ledger(request: Request):
    """Run the scheduled append now; unpublished or unconfirmed days stay pending."""
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise HTTPException(503, "数据仓库尚未就绪")
    data_dir = Path(repo.store.data_dir)
    try:
        added = _locked_append(data_dir)
    except httpx.HTTPStatusError as exc:
        logger.warning("ETF ledger manual append: source refused", exc_info=True)
        host, code = exc.request.url.host, exc.response.status_code
        raise HTTPException(502, f"数据源 {host} 暂时拒绝访问（HTTP {code}），台账未改动，请稍后重试") from exc
    except httpx.HTTPError as exc:
        logger.warning("ETF ledger manual append: source unreachable", exc_info=True)
        raise HTTPException(502, f"无法连接数据源 {exc.request.url.host}，台账未改动，请稍后重试") from exc
    except Exception as exc:
        logger.exception("ETF ledger manual append failed")
        raise HTTPException(500, "份额台账更新失败，台账未改动，详情见后端日志") from exc
    if added is None:
        raise HTTPException(409, "份额台账正在更新，请稍后刷新查看")
    latest = last_published(data_dir)
    return {
        "added": added,
        "latest": latest.isoformat() if latest else None,
        "pending": [day.isoformat() for day in pending_sessions(data_dir, cn_now().date())],
    }
