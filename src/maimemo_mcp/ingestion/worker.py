"""Nonblocking per-slot ownership, held until formal collection commits."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from time import perf_counter
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from maimemo_mcp.ingestion.normalizers import SHANGHAI, utc_instant
from maimemo_mcp.ingestion.scheduler import Schedule, ScheduledJob, advisory_key
from maimemo_mcp.ingestion.service import IngestionResult, StudyIngestionService
from maimemo_mcp.logging import log_event
from maimemo_mcp.storage.models.ingestion import IngestionRun
from maimemo_mcp.storage.repositories import StudyHistoryRepository

logger = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        service: StudyIngestionService,
        schedule: Schedule,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(SHANGHAI),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("Polling interval must be positive")
        self.service = service
        self.schedule = schedule
        self.clock = clock
        self.sleep = sleep
        self.poll_interval_seconds = poll_interval_seconds

    async def run_job(self, job: ScheduledJob, now: datetime) -> IngestionResult:
        trace_id = uuid4().hex
        started = perf_counter()
        try:
            result = await self._run_job(job, now)
        except BaseException as exc:
            log_event(
                logger,
                "worker_collection",
                endpoint=job.identity,
                latency_ms=(perf_counter() - started) * 1000,
                status="failed",
                trace_id=trace_id,
                error=exc,
            )
            raise
        log_event(
            logger,
            "worker_collection",
            endpoint=job.identity,
            latency_ms=(perf_counter() - started) * 1000,
            status=result.status,
            trace_id=trace_id,
        )
        return result

    async def _run_job(self, job: ScheduledJob, now: datetime) -> IngestionResult:
        at = utc_instant(now)
        slot = utc_instant(job.scheduled_at)
        if slot > at:
            raise ValueError("A scheduled job cannot run before its slot")
        key = advisory_key(job.identity, slot)
        async with self.service.session_factory() as session:
            connection = await session.connection()
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
            )
            if not acquired:
                return self._skip("scheduled slot owned by another worker")
            try:
                prior = await session.scalar(
                    select(IngestionRun.id).where(
                        IngestionRun.task_type == job.identity,
                        IngestionRun.scheduled_at == slot,
                        IngestionRun.status.in_(["complete", "partial"]),
                    )
                )
                if prior is not None:
                    return self._skip("scheduled slot already collected")
                if job.task_type == "today":
                    return await self.service.collect_today(at, scheduled_at=slot)
                if job.task_type == "records":
                    return await self.service.collect_records(at, scheduled_at=slot)
                assert job.study_date is not None
                async with self.service.session_factory.begin() as summary_session:
                    repo = StudyHistoryRepository(summary_session, clock=lambda: now)
                    await repo.record_daily_summary(job.study_date)
                    summary = await summary_session.scalar(
                        select(IngestionRun).where(
                            IngestionRun.task_type == job.identity,
                            IngestionRun.scheduled_at == slot,
                        )
                    )
                    if summary is None:
                        return self._skip("no formal observations for summary day")
                    result = IngestionResult(
                        summary.id,
                        "partial" if summary.status == "partial" else "complete",
                        summary.request_count,
                        summary.result_count,
                        [summary.error_summary] if summary.error_summary else [],
                    )
                return result
            finally:
                # Keep the same connection pinned until release is confirmed. Shielding
                # cleanup ensures cancellation cannot return a locked connection to the pool.
                cleanup = asyncio.create_task(self._release_lock(connection, key))
                cancelled = False
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cancelled = True
                cleanup.result()
                if cancelled:
                    raise asyncio.CancelledError

    @staticmethod
    def _skip(reason: str) -> IngestionResult:
        return IngestionResult(None, "skipped", 0, 0, [reason])

    @staticmethod
    async def _release_lock(connection: AsyncConnection, key: int) -> None:
        try:
            released = await connection.scalar(
                text("SELECT pg_advisory_unlock(:key)"), {"key": key}
            )
            if not released:
                raise RuntimeError("Worker advisory lock release was not confirmed")
        except BaseException:
            # An uncertain connection must be discarded, never pooled with a session lock.
            await connection.invalidate()
            raise

    async def run_forever(self) -> None:
        while True:
            now = utc_instant(self.clock()).astimezone(SHANGHAI)
            for job in self.schedule.next_runs(now):
                observed_at = utc_instant(self.clock()).astimezone(SHANGHAI)
                await self.run_job(job, observed_at)
            await self.sleep(self.poll_interval_seconds)
