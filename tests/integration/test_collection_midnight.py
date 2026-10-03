"""Real transactions must not persist mixed Shanghai learning days."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from maimemo.api_client.study import StudyClient
from maimemo.api_client.transport import MaimemoTransport
from maimemo.ingestion.service import StudyIngestionService
from maimemo.storage.models.ingestion import ApiSnapshot, IngestionRun
from maimemo.storage.models.learning import DailyProgress, DailyWordObservation
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

BEFORE = datetime(2030, 10, 2, 15, 59, 59, tzinfo=UTC)
AFTER = BEFORE + timedelta(seconds=2)
PROGRESS = {"progress": {"finished": 1, "total": 1, "study_time": 1000}}
TODAY = {"today_items": [{"voc_id": "midnight", "voc_spelling": "midnight",
                         "order": 1, "is_new": False, "is_finished": True}]}


@pytest.mark.parametrize("cross_at", ["progress", "today", "limiter", "retry"])
async def test_http_or_wait_crossing_midnight_rolls_back_all_today_data(
    database: AsyncEngine, cross_at: str,
) -> None:
    instant = BEFORE
    sends = 0

    class Limiter:
        async def acquire(self, fingerprint: str) -> None:
            nonlocal instant
            if cross_at == "limiter":
                instant = AFTER

    async def sleep(delay: float) -> None:
        nonlocal instant
        instant = AFTER

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal instant, sends
        sends += 1
        operation = request.url.path.rsplit("/", 1)[-1]
        if cross_at == "retry" and sends == 1:
            return httpx.Response(503)
        if (cross_at == "progress" and operation == "get_study_progress") or (
            cross_at == "today" and operation == "get_today_items"
        ):
            instant = AFTER
        return httpx.Response(200, json=PROGRESS if operation == "get_study_progress" else TODAY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = MaimemoTransport(SecretStr("synthetic-token"), SecretStr("synthetic-key"),
                                    Limiter(), client=client, sleep=sleep)
        service = StudyIngestionService(async_sessionmaker(database), StudyClient(transport))
        service.clock = lambda: instant
        result = await service.collect_today(BEFORE)
    assert result.status == "failed"
    assert result.error_category == "learning_day_changed"
    async with async_sessionmaker(database)() as session:
        for model in (DailyProgress, DailyWordObservation, ApiSnapshot):
            assert list(await session.scalars(select(model))) == []


async def test_global_lock_wait_refreshes_learning_date_before_first_request(
    database: AsyncEngine,
) -> None:
    from tests.integration.test_worker_locking import Upstream

    instant = BEFORE
    factory = async_sessionmaker(database)
    service = StudyIngestionService(factory, Upstream())
    service.clock = lambda: instant
    async with database.begin() as blocker:
        await blocker.execute(text("SELECT pg_advisory_xact_lock(724966203)"))
        pending = asyncio.create_task(service.collect_today(BEFORE))
        # Confirm PostgreSQL is actually waiting on the formal lock.
        for _ in range(100):
            async with database.connect() as connection:
                waiting = await connection.scalar(text(
                    "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted"
                ))
            if waiting:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("Formal collection never waited on its lock")
        instant = AFTER
    result = await asyncio.wait_for(pending, 3)
    assert result.status == "complete"
    async with factory() as session:
        # UTC calendar is still Oct 2; persisted learning day must be Shanghai Oct 3.
        assert (await session.scalar(select(DailyProgress.study_date))).isoformat() == "2030-10-03"
        assert await session.scalar(select(IngestionRun.started_at)) == AFTER


async def test_default_worker_reuses_its_live_clock_for_midnight_checks(
    database: AsyncEngine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import maimemo_worker.worker as worker_module
    from maimemo.api_client.models import StudyProgressResponse
    from maimemo_worker.scheduler import ScheduledJob

    from tests.integration.test_worker_locking import Upstream, schedule

    instant = BEFORE

    class LiveClock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return instant

    class MidnightUpstream(Upstream):
        async def get_progress(self) -> StudyProgressResponse:
            nonlocal instant
            instant = AFTER
            return await super().get_progress()

    monkeypatch.setattr(worker_module, "datetime", LiveClock)
    service = StudyIngestionService(async_sessionmaker(database), MidnightUpstream())
    worker = worker_module.Worker(service, schedule())
    result = await worker.run_job(ScheduledJob("today", BEFORE), BEFORE)
    assert result.status == "failed"
    assert result.error_category == "learning_day_changed"
