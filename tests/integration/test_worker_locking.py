"""Real concurrent PostgreSQL locks and persisted slots protect formal history."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from maimemo.api_client.models import (
    StudyProgressResponse,
    StudyRecordsResponse,
    TodayItemsResponse,
)
from maimemo.api_client.study import StudyRecordsRequest, TodayItemsRequest
from maimemo.config import AnalysisIntervals
from maimemo.ingestion.service import StudyIngestionService
from maimemo.storage.models.ingestion import IngestionRun
from maimemo.storage.models.learning import DailyProgress
from maimemo.storage.repositories import StudyHistoryRepository
from maimemo_worker.scheduler import Schedule, ScheduledJob
from maimemo_worker.worker import Worker
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

SHANGHAI = ZoneInfo("Asia/Shanghai")
AT = datetime(2030, 10, 2, 10, 17, tzinfo=SHANGHAI)
SLOT = datetime(2030, 10, 2, 10, tzinfo=SHANGHAI)


def schedule() -> Schedule:
    return Schedule(AnalysisIntervals())


class Upstream:
    """Only the external API is replaced; scheduler, service and locks stay real."""

    def __init__(self, *, blocked: bool = False, fail: bool = False) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.fail = fail

    async def get_progress(self) -> StudyProgressResponse:
        self.entered.set()
        await self.release.wait()
        if self.fail:
            raise OSError("synthetic upstream failure")
        return StudyProgressResponse.model_validate(
            {
                "progress": {"finished": 1, "total": 1, "study_time": 60000},
            }
        )

    async def get_today_items(self, request: TodayItemsRequest) -> TodayItemsResponse:
        return TodayItemsResponse.model_validate(
            {
                "today_items": [
                    {
                        "voc_id": "opaque",
                        "voc_spelling": "apple",
                        "order": 1,
                        "is_new": False,
                        "is_finished": True,
                    }
                ]
            }
        )

    async def query_records(self, request: StudyRecordsRequest) -> StudyRecordsResponse:
        self.entered.set()
        await self.release.wait()
        if self.fail:
            raise OSError("synthetic upstream failure")
        return StudyRecordsResponse.model_validate({"records": [], "count": 0})


def worker(database: AsyncEngine, upstream: Upstream, **kwargs: Any) -> Worker:
    service = StudyIngestionService(async_sessionmaker(database, expire_on_commit=False), upstream)
    kwargs.setdefault("clock", lambda: AT)
    return Worker(service, schedule(), **kwargs)


async def test_scoring_failure_rolls_back_history_and_success_slot_then_retries(
    database: AsyncEngine,
) -> None:
    from maimemo.storage.models.analysis import WeaknessScore
    from maimemo.storage.models.ingestion import ApiSnapshot
    from maimemo.storage.models.learning import DailyWordObservation

    job = ScheduledJob("today", SLOT)
    async with database.begin() as connection:
        await connection.execute(text(
            "ALTER TABLE weakness_score ADD CONSTRAINT reject_synthetic_score CHECK (score < 0)"
        ))
    try:
        result = await worker(database, Upstream()).run_job(job, AT)
        assert result.status == "failed"
        async with database.connect() as connection:
            for model in (WeaknessScore, ApiSnapshot, DailyWordObservation):
                assert await connection.scalar(select(func.count()).select_from(model)) == 0
            assert list(await connection.scalars(select(IngestionRun.status))) == ["failed"]
    finally:
        async with database.begin() as connection:
            await connection.execute(text(
                "ALTER TABLE weakness_score DROP CONSTRAINT reject_synthetic_score"
            ))
    assert (await worker(database, Upstream()).run_job(job, AT)).status == "complete"
    async with database.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(WeaknessScore)) == 1


@pytest.mark.parametrize("task", ["today", "records"])
async def test_two_workers_race_and_late_worker_skips_completed_slot(
    database: AsyncEngine,
    task: str,
) -> None:
    upstream = Upstream(blocked=True)
    first, second = worker(database, upstream), worker(database, Upstream())
    job = ScheduledJob("today" if task == "today" else "records", SLOT)
    pending = asyncio.create_task(first.run_job(job, AT))
    await asyncio.wait_for(upstream.entered.wait(), 3)
    try:
        same_instant = ScheduledJob(job.task_type, SLOT.astimezone(UTC))
        loser = await asyncio.wait_for(second.run_job(same_instant, AT), 3)
        assert loser.status == "skipped" and loser.run_id is None
    finally:
        upstream.release.set()
    winner = await pending
    assert winner.status == "complete"
    assert (await second.run_job(job, AT + timedelta(minutes=1))).status == "skipped"
    async with database.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(IngestionRun)) == 1
        assert await connection.scalar(select(IngestionRun.scheduled_at)) == SLOT
        assert await connection.scalar(select(IngestionRun.started_at)) == AT


async def test_failure_releases_lock_and_same_slot_can_retry(database: AsyncEngine) -> None:
    job = ScheduledJob("today", SLOT)
    assert (await worker(database, Upstream(fail=True)).run_job(job, AT)).status == "failed"
    assert (await worker(database, Upstream()).run_job(job, AT)).status == "complete"
    async with database.connect() as connection:
        assert list(
            (
                await connection.scalars(select(IngestionRun.status).order_by(IngestionRun.status))
            ).all()
        ) == ["complete", "failed"]


async def test_cancellation_releases_lock_before_connection_returns(database: AsyncEngine) -> None:
    upstream = Upstream(blocked=True)
    job = ScheduledJob("today", SLOT)
    pending = asyncio.create_task(worker(database, upstream).run_job(job, AT))
    await asyncio.wait_for(upstream.entered.wait(), 3)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert (await worker(database, Upstream()).run_job(job, AT)).status == "complete"
    async with database.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(IngestionRun)) == 1
        # No pooled connection retains a session lock after cancellation or success.
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                    "AND database=(SELECT oid FROM pg_database WHERE datname=current_database())"
                )
            )
            == 0
        )


async def test_restart_immediately_after_run_does_not_collect_again(database: AsyncEngine) -> None:
    jobs = schedule().next_runs(AT)
    first = worker(database, Upstream())
    assert [result.status for result in [await first.run_job(job, AT) for job in jobs]] == [
        "skipped",
        "complete",
        "complete",
    ]
    restarted = worker(database, Upstream())
    results = [
        await restarted.run_job(job, AT + timedelta(seconds=1))
        for job in schedule().next_runs(AT + timedelta(seconds=1))
    ]
    assert all(result.status == "skipped" for result in results)
    async with database.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(IngestionRun)) == 2


async def test_forever_uses_injected_clock_sleep_and_can_cancel(database: AsyncEngine) -> None:
    instant = AT

    async def sleep(delay: float) -> None:
        nonlocal instant
        assert delay > 0
        if instant == AT:
            instant = datetime(2030, 10, 2, 10, 30, tzinfo=SHANGHAI)
        else:
            raise asyncio.CancelledError

    collector = worker(database, Upstream(), clock=lambda: instant, sleep=sleep)
    with pytest.raises(asyncio.CancelledError):
        await collector.run_forever()
    async with database.connect() as connection:
        assert list(
            (
                await connection.scalars(
                    select(IngestionRun.task_type).order_by(
                        IngestionRun.started_at, IngestionRun.task_type
                    )
                )
            ).all()
        ) == ["records", "today", "today"]


async def test_daily_summary_idempotent_and_never_invents_unobserved_history(
    database: AsyncEngine,
) -> None:
    service = StudyIngestionService(
        async_sessionmaker(database, expire_on_commit=False), Upstream()
    )
    await service.collect_today(AT)
    async with service.session_factory.begin() as session:
        repo = StudyHistoryRepository(session, clock=lambda: AT + timedelta(days=1))
        await repo.record_daily_summary(date(2030, 10, 1))
        await repo.record_daily_summary(date(2030, 10, 2))
        await repo.record_daily_summary(date(2030, 10, 2))
    async with service.session_factory() as session:
        summary = await session.scalar(
            select(IngestionRun).where(IngestionRun.task_type.like("daily_summary:%"))
        )
        assert summary is not None and summary.task_type == "daily_summary:2030-10-02"
        assert summary.result_count == 2 and summary.request_count == 0
        assert summary.scheduled_at == datetime(2030, 10, 2, 16, tzinfo=UTC)


async def test_health_uses_formal_runs_cutoff_and_reports_missing_days(
    database: AsyncEngine,
) -> None:
    factory = async_sessionmaker(database, expire_on_commit=False)
    service = StudyIngestionService(factory, Upstream())
    await service.collect_today(AT)
    await service.collect_records(AT)
    async with factory() as session:
        repo = StudyHistoryRepository(session)
        before = await repo.get_data_health(AT - timedelta(seconds=1))
        assert before.completeness == "unavailable" and before.data_through is None
        fresh = await repo.get_data_health(AT)
        assert fresh.completeness == "complete"
        assert fresh.data_through == date(2030, 10, 2)
        stale = await repo.get_data_health(AT + timedelta(days=2))
        assert stale.completeness == "stale"
        assert stale.missing_dates == [date(2030, 10, 3)]


async def test_worker_summary_commits_then_releases_ownership(database: AsyncEngine) -> None:
    first = worker(database, Upstream())
    await first.run_job(ScheduledJob("today", SLOT), AT)
    end = datetime(2030, 10, 3, tzinfo=SHANGHAI)
    summary = ScheduledJob("daily_summary", end, date(2030, 10, 2))
    outcomes = await asyncio.gather(
        first.run_job(summary, end),
        worker(database, Upstream()).run_job(summary, end),
    )
    assert sorted(result.status for result in outcomes) == ["complete", "skipped"]
    assert next(result for result in outcomes if result.status == "complete").result_count == 2
    assert (await worker(database, Upstream()).run_job(summary, end)).status == "skipped"
    async with database.connect() as connection:
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                    "AND database=(SELECT oid FROM pg_database WHERE datname=current_database())"
                )
            )
            == 0
        )


async def test_direct_service_duplicate_slot_is_skipped_without_failed_run(
    database: AsyncEngine,
) -> None:
    service = StudyIngestionService(async_sessionmaker(database), Upstream())
    assert (await service.collect_today(AT, scheduled_at=SLOT)).status == "complete"
    duplicate = await service.collect_today(AT + timedelta(seconds=1), scheduled_at=SLOT)
    assert duplicate.status == "skipped" and duplicate.run_id is None
    async with database.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(IngestionRun)) == 1


async def test_health_failed_runs_and_custom_freshness_are_explicit(database: AsyncEngine) -> None:
    service = StudyIngestionService(async_sessionmaker(database), Upstream())
    await service.collect_today(AT)
    await service.collect_records(AT)
    service.client = Upstream(fail=True)
    await service.collect_today(AT + timedelta(minutes=1))
    await service.collect_today(AT + timedelta(minutes=2))
    async with service.session_factory() as session:
        repo = StudyHistoryRepository(session, today_interval=timedelta(minutes=15))
        health = await repo.get_data_health(AT + timedelta(minutes=2))
        assert health.completeness == "partial"
        assert health.tasks["today"].consecutive_failures == 2
        assert health.tasks["today"].latest_status == "failed"
        assert health.tasks["today"].data_through == AT
        assert (await repo.get_data_health(AT + timedelta(minutes=15))).completeness == "partial"
        assert (
            await repo.get_data_health(AT + timedelta(minutes=15, microseconds=1))
        ).completeness == "stale"
        with pytest.raises(ValueError, match="timezone"):
            await repo.get_data_health(datetime(2030, 10, 2))


async def test_health_exposes_running_formal_job_without_claiming_success(
    database: AsyncEngine,
) -> None:
    async with async_sessionmaker(database).begin() as session:
        session.add(
            IngestionRun(
                task_type="today",
                status="running",
                started_at=AT,
                request_count=0,
                result_count=0,
            )
        )
    async with async_sessionmaker(database)() as session:
        health = await StudyHistoryRepository(session).get_data_health(AT)
        assert health.completeness == "unavailable"
        assert health.tasks["today"].latest_status == "running"
        assert health.tasks["today"].last_success_at is None


async def test_partial_summary_is_idempotent_and_rejects_unfinished_day(
    database: AsyncEngine,
) -> None:
    class EmptyDay(Upstream):
        async def get_today_items(self, request: TodayItemsRequest) -> TodayItemsResponse:
            return TodayItemsResponse.model_validate({"today_items": []})

    service = StudyIngestionService(async_sessionmaker(database), EmptyDay())
    assert (await service.collect_today(AT)).status == "partial"
    async with service.session_factory.begin() as session:
        with pytest.raises(ValueError, match="finished"):
            await StudyHistoryRepository(session, clock=lambda: AT).record_daily_summary(AT.date())
        repo = StudyHistoryRepository(session, clock=lambda: AT + timedelta(days=1))
        await repo.record_daily_summary(AT.date())
        await repo.record_daily_summary(AT.date())
    async with service.session_factory() as session:
        summary = await session.scalar(
            select(IngestionRun).where(IngestionRun.task_type == "daily_summary:2030-10-02")
        )
        assert summary is not None and summary.status == "partial" and summary.result_count == 1


async def test_cancel_while_database_has_lock_but_response_is_pending(
    database: AsyncEngine,
) -> None:
    # Delay only the real PostgreSQL response after taking the real session lock;
    # this exposes the ownership gap without replacing or faking the lock itself.
    def delay_lock_response(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        many: bool,
    ) -> tuple[str, Any]:
        if "pg_try_advisory_lock" in statement:
            statement = (
                "WITH held AS MATERIALIZED (" + statement + " AS locked) "
                "SELECT locked FROM held CROSS JOIN LATERAL "
                "(SELECT pg_sleep(CASE WHEN locked THEN 1.0 ELSE 0 END)) AS delay"
            )
        return statement, parameters

    event.listen(database.sync_engine, "before_cursor_execute", delay_lock_response, retval=True)
    pending = asyncio.create_task(
        worker(database, Upstream()).run_job(ScheduledJob("today", SLOT), AT)
    )
    try:
        for _ in range(100):
            async with database.connect() as connection:
                count = await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted "
                        "AND database=(SELECT oid FROM pg_database "
                        "WHERE datname=current_database())"
                    )
                )
            if count == 1:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("Real PostgreSQL lock was never acquired")
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        async with database.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                        "AND database=(SELECT oid FROM pg_database "
                        "WHERE datname=current_database())"
                    )
                )
                == 0
            )
    finally:
        event.remove(database.sync_engine, "before_cursor_execute", delay_lock_response)
        if not pending.done():
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass
        await database.dispose()


async def test_forever_samples_actual_clock_for_each_collection(database: AsyncEngine) -> None:
    instant = AT

    class SlowToday(Upstream):
        async def get_today_items(self, request: TodayItemsRequest) -> TodayItemsResponse:
            nonlocal instant
            instant = AT + timedelta(minutes=1)
            return await super().get_today_items(request)

    async def stop(delay: float) -> None:
        raise asyncio.CancelledError

    collector = worker(database, SlowToday(), clock=lambda: instant, sleep=stop)
    with pytest.raises(asyncio.CancelledError):
        await collector.run_forever()
    async with database.connect() as connection:
        assert await connection.scalar(
            select(IngestionRun.started_at).where(IngestionRun.task_type == "records")
        ) == AT + timedelta(minutes=1)


async def test_health_does_not_infer_missing_history_from_future_formal_commit(
    database: AsyncEngine,
) -> None:
    factory = async_sessionmaker(database)
    async with factory.begin() as session:
        session.add(
            IngestionRun(
                task_type="today",
                status="complete",
                started_at=AT - timedelta(days=1),
                finished_at=AT + timedelta(days=2),
                request_count=0,
                result_count=0,
            )
        )
        session.add(
            DailyProgress(
                study_date=date(2030, 10, 1),
                completed_count=1,
                total_count=1,
                study_seconds=60,
                completeness="complete",
                first_observed_at=AT - timedelta(days=1),
                last_observed_at=AT - timedelta(days=1),
            )
        )
    async with factory() as session:
        health = await StudyHistoryRepository(session).get_data_health(AT + timedelta(days=1))
        assert health.completeness == "unavailable" and health.data_through is None
        assert health.missing_dates == []


async def test_unique_slot_conflict_rolls_back_collection_and_returns_skip(
    database: AsyncEngine,
) -> None:
    # A separate real SQL writer simulates a success committed after the pre-check.
    # The index must remain the final defense even for a writer missing advisory locks.
    upstream = Upstream(blocked=True)
    factory = async_sessionmaker(database)
    service = StudyIngestionService(factory, upstream)
    pending = asyncio.create_task(service.collect_today(AT, scheduled_at=SLOT))
    await asyncio.wait_for(upstream.entered.wait(), 3)
    try:
        async with factory.begin() as session:
            session.add(
                IngestionRun(
                    task_type="today",
                    started_at=AT,
                    finished_at=AT,
                    scheduled_at=SLOT,
                    status="complete",
                    request_count=0,
                    result_count=0,
                )
            )
    finally:
        upstream.release.set()
    result = await pending
    assert result.status == "skipped" and result.run_id is None
    async with database.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(IngestionRun)) == 1
        assert await connection.scalar(select(IngestionRun.status)) == "complete"
        assert await connection.scalar(text("SELECT count(*) FROM api_snapshot")) == 0
