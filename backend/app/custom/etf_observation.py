"""Read-only overview and source drill-down for the published ETF ledger.

Page requests only read. A scheduled job appends newly published sessions.
"""

import logging
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.custom.etf_observation_data import turnover
from app.custom.etf_observation_data.daily import append_new_sessions
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


def _append(data_dir: Path) -> None:
    try:
        added = append_new_sessions(Path(data_dir), cn_now().date())
        if added:
            logger.info("ETF ledger appended %s", ", ".join(added))
    except Exception:
        # Source outages or a conflicting revision leave the release untouched; retry next run.
        logger.exception("ETF ledger daily append failed")


@asynccontextmanager
async def _lifespan(app):
    scheduler = getattr(app.state, "scheduler", None)
    registered = False
    if scheduler is not None:
        try:
            # Evening for same-day units and NAVs; morning catches regulator reports published overnight.
            scheduler.add_job(
                _append, "cron", args=[app.state.repo.store.data_dir],
                hour="8,21", minute=30, timezone="Asia/Shanghai",
                id=_APPEND_JOB, replace_existing=True, coalesce=True, max_instances=1, misfire_grace_time=3600,
            )
            registered = True
        except Exception:
            logger.exception("ETF 份额台账定时追加未能启动")
    try:
        yield
    finally:
        if registered and scheduler.get_job(_APPEND_JOB) is not None:
            scheduler.remove_job(_APPEND_JOB)


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
