"""Post-close Huichun archival driven by successful stock daily pipeline receipts."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from contextlib import nullcontext
from datetime import date, datetime, time

from app.market_time import CN_TZ, cn_now
from app.services.data_integrity import CLOSE_CUTOFF

logger = logging.getLogger(__name__)


class ArchiveInterruptedError(Exception):
    """Stop a superseded/closing confirmation between provider chunks."""


def completed_sync_day(job: dict, now: datetime) -> date | None:
    """Only replay receipts from today's post-close full stock pipeline."""
    if job.get("status") != "succeeded" or not isinstance(job.get("result"), dict):
        return None
    try:
        day = date.fromisoformat(job["result"]["stock_daily_sync_date"])
        finished = datetime.fromisoformat(job["finished_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if finished.tzinfo is None:
        return None
    cutoff = datetime.combine(day, CLOSE_CUTOFF, CN_TZ)
    if day == now.date() and cutoff <= finished <= now:
        return day
    return None


class HuichunArchiveSchedule:
    """Coalesce pipeline wakes on a single worker. No thread while idle."""

    def __init__(
        self, service, *, repair: Callable[[date, Callable[[int, int], None]], str | None],
        retry_delays=(60, 180, 600),
    ):
        self.service = service
        self.repair = repair
        self._delays = retry_delays
        self._condition = threading.Condition()
        self._latest_receipt: str | None = None
        self._pending: date | None = None
        self._closed = False
        self._thread: threading.Thread | None = None

    def resume(self, recent_jobs: Iterable[dict]) -> None:
        for job in recent_jobs:
            if completed_sync_day(job, cn_now()) is not None:
                self.offer(job)
                return

    def offer(self, job: dict) -> None:
        day = completed_sync_day(job, cn_now())
        if day is None or not job.get("id"):
            return
        with self._condition:
            if self._closed or self._latest_receipt == job["id"]:
                return
            self._latest_receipt = job["id"]
            self._pending = day
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="huichun-post-close", daemon=True)
                self._thread.start()
            self._condition.notify_all()

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._closed or self._pending is None:
                    self._thread = None
                    return
                day, self._pending = self._pending, None
            self._attempts(day)

    def _retry_windows(self, day: date):
        yield from (0, *self._delays)
        # The close pipeline can remain busy beyond the short backoff. Keep two
        # bounded chances later today; a missed day must stay a historical backfill.
        for hour in (18, 20):
            now = cn_now()
            if now.date() != day:
                return
            delay = (datetime.combine(day, time(hour), CN_TZ) - now).total_seconds()
            if delay > 0:
                yield delay

    def _attempts(self, day: date) -> None:
        outcome = "retry"
        for delay in self._retry_windows(day):
            with self._condition:
                if self._condition.wait_for(lambda: self._closed or self._pending is not None, timeout=delay):
                    return
            if day != cn_now().date():
                return
            try:
                if outcome == "waiting":
                    if self.repair(day, self._check_running) == "busy":
                        logger.info("回春 %s 定向核准通道占用，等待后续补查", day)
                    self._check_running(0, 0)
                outcome = self.service.try_auto_archive(day)
                if outcome in {"done", "skip"}:
                    return
            except ArchiveInterruptedError:
                return
            except Exception:
                logger.exception("回春 %s 盘后归档或定向核准失败", day)
                try:
                    self.service.record_daily_waiting(day, "盘后核准或归档暂时失败，等待补查")
                except Exception:
                    logger.exception("回春 %s 等待状态未能保存", day)
        logger.info("回春 %s 已达本次同步的补查次数上限", day)

    def _check_running(self, _done: int, _total: int) -> None:
        with self._condition:
            if self._closed or self._pending is not None:
                raise ArchiveInterruptedError()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            thread = self._thread
        if thread:
            thread.join(timeout=5)


def repair_daily_evidence(service, state, day: date, on_chunk_done=None) -> str | None:
    """Re-read only unresolved symbols from the routed daily source. Do not recurse into the pipeline."""
    from app.services import kline_sync
    from app.services.heavy_job_limiter import HeavyJobLimitTimeoutError, shared_heavy_job_limiter
    from app.services.pipeline_jobs import release_run_slot, try_acquire_run_slot

    owner = f"huichun-confirm:{day}"
    if not try_acquire_run_slot(owner):
        return "busy"
    try:
        with shared_heavy_job_limiter.slot("exclusive", timeout=0):
            if cn_now().date() != day:
                return
            symbols = service.daily_preflight(day)["needs_symbols"]
            if symbols:
                qs = getattr(state, "quote_service", None)
                with qs.paused() if qs is not None else nullcontext():
                    kline_sync.confirm_daily_session(
                        state.repo, state.capabilities, day, symbols, on_chunk_done=on_chunk_done,
                    )
    except HeavyJobLimitTimeoutError:
        # A user-initiated write or rebuild takes priority over this bounded retry.
        return "busy"
    finally:
        release_run_slot(owner)
