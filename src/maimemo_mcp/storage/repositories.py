"""Session-bound persistence; callers own commit/rollback and formal write authority."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maimemo_mcp.ingestion.hashing import stable_payload_hash
from maimemo_mcp.ingestion.normalizers import (
    SHANGHAI,
    DailyProgressInput,
    DailyWordObservationInput,
    StudyRecordSnapshotInput,
    utc_instant,
)
from maimemo_mcp.ingestion.scheduler import advisory_key
from maimemo_mcp.storage.models.ingestion import ApiSnapshot, IngestionRun
from maimemo_mcp.storage.models.learning import (
    DailyProgress,
    DailyWordObservation,
    StudyRecordSnapshot,
    Vocabulary,
)

Completeness = Literal["complete", "partial", "stale", "unavailable"]


@dataclass(frozen=True)
class TaskHealth:
    completeness: Completeness
    latest_status: str | None
    last_success_at: datetime | None
    data_through: datetime | None
    consecutive_failures: int


@dataclass(frozen=True)
class DataHealth:
    completeness: Completeness
    data_through: date | None
    last_success_at: datetime | None
    tasks: dict[str, TaskHealth]
    missing_dates: list[date] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class StudyHistoryRepository:
    def __init__(
        self,
        session: AsyncSession,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        today_interval: timedelta = timedelta(minutes=30),
        records_interval: timedelta = timedelta(hours=2),
    ) -> None:
        self.session = session
        self.clock = clock
        self.today_interval = today_interval
        self.records_interval = records_interval

    async def get_data_health(self, now: datetime) -> DataHealth:
        at = utc_instant(now)
        runs = list(
            (
                await self.session.scalars(
                    select(IngestionRun)
                    .where(
                        IngestionRun.task_type.in_(["today", "records"]),
                        IngestionRun.started_at <= at,
                        (IngestionRun.finished_at <= at) | IngestionRun.finished_at.is_(None),
                    )
                    .order_by(IngestionRun.started_at.desc(), IngestionRun.finished_at.desc())
                )
            ).all()
        )
        tasks: dict[str, TaskHealth] = {}
        successes: list[IngestionRun] = []
        warnings: list[str] = []
        for task, interval in (("today", self.today_interval), ("records", self.records_interval)):
            stream = [run for run in runs if run.task_type == task]
            terminal = [run for run in stream if run.finished_at is not None]
            success = next((run for run in terminal if run.status in ("complete", "partial")), None)
            failures = 0
            for run in terminal:
                if run.status != "failed":
                    break
                failures += 1
            state: Completeness = "unavailable"
            if success is not None:
                successes.append(success)
                state = (
                    "partial"
                    if (success.status == "partial" or failures or stream[0].status == "running")
                    else "complete"
                )
                if at - success.started_at > interval:
                    state = "stale"
            tasks[task] = TaskHealth(
                state,
                stream[0].status if stream else None,
                success.finished_at if success else None,
                success.started_at if success else None,
                failures,
            )
            if state != "complete":
                warnings.append(f"{task}: {state}; consecutive failures={failures}")
        states = [task.completeness for task in tasks.values()]
        overall: Completeness = "complete"
        if all(state == "unavailable" for state in states):
            overall = "unavailable"
        elif "stale" in states:
            overall = "stale"
        elif any(state != "complete" for state in states):
            overall = "partial"
        observed_days = {
            run.started_at.astimezone(SHANGHAI).date()
            for run in runs
            if run.task_type == "today"
            and run.finished_at is not None
            and run.status in ("complete", "partial")
        }
        dates = list(
            (
                await self.session.scalars(
                    select(DailyProgress.study_date)
                    .where(
                        DailyProgress.first_observed_at <= at,
                        DailyProgress.study_date.in_(observed_days),
                    )
                    .order_by(DailyProgress.study_date)
                )
            ).all()
        )
        missing = []
        if dates:
            day = dates[0]
            present = set(dates)
            while day < at.astimezone(SHANGHAI).date():
                if day not in present:
                    missing.append(day)
                day += timedelta(days=1)
        if missing:
            warnings.append("observed history has missing learning days")
            if overall == "complete":
                overall = "partial"
        return DataHealth(
            overall,
            max((run.started_at.astimezone(SHANGHAI).date() for run in successes), default=None),
            max(
                (run.finished_at for run in successes if run.finished_at is not None), default=None
            ),
            tasks,
            missing,
            warnings,
        )

    async def record_daily_summary(self, day: date) -> None:
        slot = datetime.combine(day + timedelta(days=1), time.min, SHANGHAI).astimezone(UTC)
        at = utc_instant(self.clock())
        if at < slot:
            raise ValueError("Daily summary requires a finished Shanghai learning day")
        task = f"daily_summary:{day.isoformat()}"
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {
                "key": advisory_key(f"summary-repository:{task}", slot),
            },
        )
        if (
            await self.session.scalar(
                select(IngestionRun.id).where(
                    IngestionRun.task_type == task,
                    IngestionRun.scheduled_at == slot,
                    IngestionRun.status.in_(["complete", "partial"]),
                )
            )
            is not None
        ):
            return
        progress = await self.session.scalar(
            select(DailyProgress).where(
                DailyProgress.study_date == day,
            )
        )
        observations = (
            await self.session.scalar(
                select(func.count())
                .select_from(DailyWordObservation)
                .where(DailyWordObservation.study_date == day)
            )
            or 0
        )
        if progress is None and observations == 0:
            return
        partial = progress is None or progress.completeness != "complete"
        run = IngestionRun(
            task_type=task,
            scheduled_at=slot,
            started_at=at,
            finished_at=at,
            status="partial" if partial else "complete",
            request_count=0,
            result_count=observations + int(progress is not None),
            error_category="incomplete" if partial else None,
            error_summary="summary source is incomplete" if partial else None,
        )
        self.session.add(run)
        await self.session.flush()

    async def snapshot(
        self,
        endpoint: str,
        request: Mapping[str, Any],
        response: Mapping[str, Any],
        at: datetime,
        run_id: UUID,
    ) -> ApiSnapshot:
        content_hash = stable_payload_hash(endpoint, request, response)
        request_hash = stable_payload_hash(endpoint, request, {})
        exists = await self.session.scalar(
            select(ApiSnapshot.id).where(ApiSnapshot.endpoint == endpoint)
        )
        statement = (
            insert(ApiSnapshot)
            .values(
                id=uuid4(),
                endpoint=endpoint,
                request_hash=request_hash,
                content_hash=content_hash,
                raw_response=dict(response),
                fetched_at=at,
                ingestion_run_id=run_id,
                observation_kind="BASELINE" if exists is None else "OBSERVATION",
            )
            .on_conflict_do_nothing(constraint="uq_api_snapshot_content")
            .returning(ApiSnapshot)
        )
        snapshot = await self.session.scalar(statement)
        if snapshot is None:
            snapshot = await self.session.scalar(
                select(ApiSnapshot).where(
                    ApiSnapshot.endpoint == endpoint,
                    ApiSnapshot.request_hash == request_hash,
                    ApiSnapshot.content_hash == content_hash,
                )
            )
        assert snapshot is not None
        return snapshot

    async def _words(
        self,
        rows: list[DailyWordObservationInput] | list[StudyRecordSnapshotInput],
    ) -> dict[str, UUID]:
        if not rows:
            return {}
        by_id = {row.maimemo_id: row for row in rows}
        statement = insert(Vocabulary).values(
            [
                {
                    "id": uuid4(),
                    "maimemo_id": row.maimemo_id,
                    "spelling": row.spelling,
                    "normalized_spelling": row.spelling.strip().casefold(),
                    "first_seen_at": row.observed_at,
                    "last_seen_at": row.observed_at,
                }
                for row in by_id.values()
            ]
        )
        returning = statement.on_conflict_do_update(
            index_elements=[Vocabulary.maimemo_id],
            set_={
                "spelling": statement.excluded.spelling,
                "normalized_spelling": statement.excluded.normalized_spelling,
                "first_seen_at": func.least(
                    Vocabulary.first_seen_at, statement.excluded.first_seen_at
                ),
                "last_seen_at": func.greatest(
                    Vocabulary.last_seen_at, statement.excluded.last_seen_at
                ),
            },
        ).returning(Vocabulary.maimemo_id, Vocabulary.id)
        return {row.maimemo_id: row.id for row in (await self.session.execute(returning)).all()}

    async def progress(self, row: DailyProgressInput, completeness: str) -> None:
        statement = insert(DailyProgress).values(
            id=uuid4(),
            study_date=row.study_date,
            completed_count=row.completed_count,
            total_count=row.total_count,
            study_seconds=row.study_seconds,
            completeness=completeness,
            first_observed_at=row.observed_at,
            last_observed_at=row.observed_at,
        )
        await self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[DailyProgress.study_date],
                set_={
                    "completed_count": row.completed_count,
                    "total_count": row.total_count,
                    "study_seconds": row.study_seconds,
                    "completeness": completeness,
                    "last_observed_at": row.observed_at,
                },
                where=DailyProgress.last_observed_at <= row.observed_at,
            )
        )

    async def today(self, rows: list[DailyWordObservationInput], snapshot_id: UUID) -> None:
        words = await self._words(rows)
        # Duplicate upstream IDs must not make one INSERT affect a row twice.
        unique = {row.maimemo_id: row for row in rows}
        if not unique:
            return
        statement = insert(DailyWordObservation).values(
            [
                {
                    "id": uuid4(),
                    "study_date": row.study_date,
                    "vocabulary_id": words[row.maimemo_id],
                    "source_snapshot_id": snapshot_id,
                    "first_feedback": row.first_feedback,
                    "is_new": row.is_new,
                    "is_complete": row.is_complete,
                    "first_observed_at": row.observed_at,
                    "last_observed_at": row.observed_at,
                }
                for row in unique.values()
            ]
        )
        await self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_daily_word_observation_day_word_source",
                set_={
                    "first_observed_at": func.least(
                        DailyWordObservation.first_observed_at, statement.excluded.first_observed_at
                    ),
                    "last_observed_at": func.greatest(
                        DailyWordObservation.last_observed_at, statement.excluded.last_observed_at
                    ),
                },
            )
        )

    async def records(self, rows: list[StudyRecordSnapshotInput], snapshot_id: UUID) -> None:
        words = await self._words(rows)
        unique = {row.maimemo_id: row for row in rows}
        if not unique:
            return
        statement = insert(StudyRecordSnapshot).values(
            [
                {
                    "id": uuid4(),
                    "vocabulary_id": words[row.maimemo_id],
                    "observed_at": row.observed_at,
                    "added_at": row.added_at,
                    "first_studied_at": row.first_studied_at,
                    "last_studied_at": row.last_studied_at,
                    "next_study_at": row.next_study_at,
                    "last_feedback": row.last_feedback,
                    "study_count": row.study_count,
                    "tags": row.tags,
                    "source_snapshot_id": snapshot_id,
                }
                for row in unique.values()
            ]
        )
        await self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_study_record_snapshot_source",
                set_={
                    "observed_at": func.greatest(
                        StudyRecordSnapshot.observed_at, statement.excluded.observed_at
                    )
                },
            )
        )
