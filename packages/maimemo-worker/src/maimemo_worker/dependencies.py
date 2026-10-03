"""Worker resource ownership and service assembly."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from maimemo.analysis.service import WeaknessService
from maimemo.application import open_database, open_upstream
from maimemo.ingestion.service import StudyIngestionService
from sqlalchemy.ext.asyncio import AsyncEngine

from maimemo_worker.config import WorkerSettings
from maimemo_worker.scheduler import Schedule


@dataclass(frozen=True, repr=False)
class WorkerDependencies:
    engine: AsyncEngine
    service: StudyIngestionService
    schedule: Schedule


@asynccontextmanager
async def open_worker_dependencies(
    settings: WorkerSettings,
) -> AsyncIterator[WorkerDependencies]:
    async with open_database(settings.core.database) as database:
        async with open_upstream(settings.upstream, database.sessions) as upstream:
            weakness = WeaknessService(
                database.sessions,
                today_interval=settings.intervals.today_interval,
                records_interval=settings.intervals.records_interval,
            )
            service = StudyIngestionService(
                database.sessions,
                upstream.study,
                weakness=weakness,
                clock=lambda: datetime.now(UTC),
            )
            yield WorkerDependencies(database.engine, service, Schedule(settings.intervals))
