"""Session-bound persistence; callers own commit/rollback and formal write authority."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from hashlib import blake2b
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import case, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maimemo_mcp.analysis.models import (
    DailyEvidence,
    RecentResponse,
    WeaknessAnalysisState,
    WeaknessEvidence,
    WeaknessResult,
    WeakWordQuery,
)
from maimemo_mcp.config import DEFAULT_RECORDS_INTERVAL_MINUTES, DEFAULT_TODAY_INTERVAL_MINUTES
from maimemo_mcp.feedback.models import FeedbackQuery, normalize_spelling
from maimemo_mcp.ingestion.hashing import stable_payload_hash
from maimemo_mcp.ingestion.normalizers import (
    SHANGHAI,
    DailyProgressInput,
    DailyWordObservationInput,
    StudyRecordSnapshotInput,
    utc_instant,
)
from maimemo_mcp.ingestion.scheduler import advisory_key
from maimemo_mcp.maimemo_client.models import Vocabulary as UpstreamVocabulary
from maimemo_mcp.storage.models.analysis import WeaknessScore
from maimemo_mcp.storage.models.feedback import LearningFeedbackEvent
from maimemo_mcp.storage.models.ingestion import ApiSnapshot, IngestionRun
from maimemo_mcp.storage.models.learning import (
    DailyProgress,
    DailyWordObservation,
    StudyRecordSnapshot,
    Vocabulary,
)

Completeness = Literal["complete", "partial", "stale", "unavailable"]


class FeedbackRepository:
    """Feedback writes only append; callers own the surrounding transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def lock(self, identity: str) -> None:
        key = int.from_bytes(
            blake2b(f"feedback:{identity}".encode(), digest_size=8).digest(), "big", signed=True
        )
        await self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    async def get(self, identity: UUID) -> LearningFeedbackEvent | None:
        return await self.session.get(LearningFeedbackEvent, identity)

    async def by_key(self, key: str) -> LearningFeedbackEvent | None:
        return await self.session.scalar(
            select(LearningFeedbackEvent).where(LearningFeedbackEvent.idempotency_key == key)
        )

    async def is_retracted(self, identity: UUID) -> bool:
        return bool(
            await self.session.scalar(
                select(LearningFeedbackEvent.id)
                .where(
                    LearningFeedbackEvent.event_type == "RETRACTION",
                    LearningFeedbackEvent.retracted_event_id == identity,
                )
                .limit(1)
            )
        )

    async def append(self, **payload: Any) -> LearningFeedbackEvent:
        identity = await self.session.scalar(
            insert(LearningFeedbackEvent)
            .values(id=uuid4(), **payload)
            .on_conflict_do_nothing(index_elements=[LearningFeedbackEvent.idempotency_key])
            .returning(LearningFeedbackEvent.id)
        )
        event = (
            await self.get(identity)
            if identity is not None
            else await self.by_key(payload["idempotency_key"])
        )
        if event is None:
            raise RuntimeError("Feedback insert produced no retained event")
        return event

    async def find_word(self, normalized_spelling: str) -> UUID | None:
        identities = list(
            await self.session.scalars(
                select(Vocabulary.id)
                .where(Vocabulary.normalized_spelling == normalized_spelling)
                .limit(2)
            )
        )
        # Ambiguous homographs do not earn an arbitrary vocabulary identity.
        return identities[0] if len(identities) == 1 else None

    async def retain_word(self, word: UpstreamVocabulary) -> UUID | None:
        await self.session.execute(
            insert(Vocabulary)
            .values(
                id=uuid4(),
                maimemo_id=word.id,
                spelling=word.spelling,
                normalized_spelling=normalize_spelling(word.spelling),
            )
            .on_conflict_do_nothing(index_elements=[Vocabulary.maimemo_id])
        )
        existing = await self.session.scalar(
            select(Vocabulary).where(Vocabulary.maimemo_id == word.id)
        )
        if existing is None or existing.normalized_spelling != normalize_spelling(word.spelling):
            return None
        return existing.id

    async def active(self, query: FeedbackQuery) -> list[LearningFeedbackEvent]:
        retraction = LearningFeedbackEvent.__table__.alias("retraction")
        statement = select(LearningFeedbackEvent).where(
            LearningFeedbackEvent.event_type == "CONFUSION",
            ~select(retraction.c.id)
            .where(
                retraction.c.event_type == "RETRACTION",
                retraction.c.retracted_event_id == LearningFeedbackEvent.id,
            )
            .exists(),
        )
        for name in ("direction", "relation_type", "evidence_type"):
            value = getattr(query, name)
            if value is not None:
                statement = statement.where(getattr(LearningFeedbackEvent, name) == value)
        if query.word_id is not None:
            statement = statement.where(
                (LearningFeedbackEvent.word_a_id == query.word_id)
                | (LearningFeedbackEvent.word_b_id == query.word_id)
            )
        statement = statement.order_by(LearningFeedbackEvent.created_at, LearningFeedbackEvent.id)
        if query.word_spelling is None:
            return list(await self.session.scalars(statement.limit(query.limit)))
        matching = normalize_spelling(query.word_spelling)
        # casefold is Python's Unicode match contract; SQL lower() is not equivalent.
        results: list[LearningFeedbackEvent] = []
        async for event in await self.session.stream_scalars(statement):
            if matching in (
                normalize_spelling(event.word_a_spelling or ""),
                normalize_spelling(event.word_b_spelling or ""),
            ):
                results.append(event)
                if len(results) == query.limit:
                    break
        return results


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
        today_interval: timedelta = timedelta(minutes=DEFAULT_TODAY_INTERVAL_MINUTES),
        records_interval: timedelta = timedelta(minutes=DEFAULT_RECORDS_INTERVAL_MINUTES),
    ) -> None:
        self.session = session
        self.clock = clock
        self.today_interval = today_interval
        self.records_interval = records_interval

    async def daily_progress(self, day: date, as_of: datetime) -> DailyProgress | None:
        # This table retains only the latest progress; never expose a future version.
        return await self.session.scalar(
            select(DailyProgress).where(
                DailyProgress.study_date == day,
                DailyProgress.last_observed_at <= utc_instant(as_of),
            )
        )

    async def local_words(self, spelling: str, as_of: datetime) -> list[Vocabulary]:
        return list(
            await self.session.scalars(
                select(Vocabulary)
                .where(
                    Vocabulary.normalized_spelling == normalize_spelling(spelling),
                    Vocabulary.first_seen_at <= utc_instant(as_of),
                )
                .order_by(Vocabulary.id)
            )
        )

    async def latest_today_words(
        self, day: date, as_of: datetime
    ) -> list[tuple[Vocabulary, DailyWordObservation]]:
        at = utc_instant(as_of)
        effective = case(
            (DailyWordObservation.last_observed_at <= at, DailyWordObservation.last_observed_at),
            else_=DailyWordObservation.first_observed_at,
        )
        latest = (
            select(
                DailyWordObservation.id,
                func.row_number()
                .over(
                    partition_by=DailyWordObservation.vocabulary_id,
                    order_by=(effective.desc(), ApiSnapshot.id.desc()),
                )
                .label("position"),
            )
            .join(ApiSnapshot, DailyWordObservation.source_snapshot_id == ApiSnapshot.id)
            .join(IngestionRun, ApiSnapshot.ingestion_run_id == IngestionRun.id)
            .where(
                DailyWordObservation.study_date == day,
                DailyWordObservation.first_observed_at <= at,
                ApiSnapshot.fetched_at <= at,
                IngestionRun.finished_at <= at,
                IngestionRun.status.in_(["complete", "partial"]),
            )
            .subquery()
        )
        rows = await self.session.execute(
            select(Vocabulary, DailyWordObservation)
            .join(DailyWordObservation, DailyWordObservation.vocabulary_id == Vocabulary.id)
            .join(latest, latest.c.id == DailyWordObservation.id)
            .where(latest.c.position == 1, Vocabulary.first_seen_at <= at)
            .order_by(Vocabulary.id)
        )
        return [(word, observation) for word, observation in rows.all()]

    async def latest_records(self, as_of: datetime) -> list[tuple[Vocabulary, StudyRecordSnapshot]]:
        at = utc_instant(as_of)
        effective = case(
            (StudyRecordSnapshot.observed_at <= at, StudyRecordSnapshot.observed_at),
            else_=ApiSnapshot.fetched_at,
        )
        latest = (
            select(
                StudyRecordSnapshot.id,
                func.row_number()
                .over(
                    partition_by=StudyRecordSnapshot.vocabulary_id,
                    order_by=(effective.desc(), ApiSnapshot.id.desc()),
                )
                .label("position"),
            )
            .join(ApiSnapshot, StudyRecordSnapshot.source_snapshot_id == ApiSnapshot.id)
            .join(IngestionRun, ApiSnapshot.ingestion_run_id == IngestionRun.id)
            .where(
                ApiSnapshot.fetched_at <= at,
                IngestionRun.finished_at <= at,
                IngestionRun.status.in_(["complete", "partial"]),
            )
            .subquery()
        )
        rows = await self.session.execute(
            select(Vocabulary, StudyRecordSnapshot)
            .join(StudyRecordSnapshot, StudyRecordSnapshot.vocabulary_id == Vocabulary.id)
            .join(latest, latest.c.id == StudyRecordSnapshot.id)
            .where(latest.c.position == 1, Vocabulary.first_seen_at <= at)
            .order_by(Vocabulary.id)
        )
        return [(word, record) for word, record in rows.all()]

    async def weakness_evidence(self, as_of: datetime) -> list[WeaknessEvidence]:
        """Choose visible versions; deduplicated response refreshes are not review events.

        When latest observation is beyond cutoff, immutable source time is the only
        retained earlier observation. Intermediate identical observations cannot be
        reconstructed from this compressed history.
        """
        at = utc_instant(as_of)
        health = await self.get_data_health(at)
        visible = (
            ApiSnapshot.fetched_at <= at,
            IngestionRun.finished_at <= at,
            IngestionRun.status.in_(["complete", "partial"]),
        )
        daily_rows = (
            await self.session.execute(
                select(DailyWordObservation, ApiSnapshot)
                .join(ApiSnapshot, DailyWordObservation.source_snapshot_id == ApiSnapshot.id)
                .join(IngestionRun, ApiSnapshot.ingestion_run_id == IngestionRun.id)
                .where(
                    *visible,
                    DailyWordObservation.first_observed_at <= at,
                    DailyWordObservation.study_date <= at.astimezone(SHANGHAI).date(),
                )
            )
        ).all()
        days: dict[UUID, dict[date, tuple[datetime, UUID, DailyWordObservation]]] = {}
        for row, source in daily_rows:
            effective = (
                row.last_observed_at if row.last_observed_at <= at else row.first_observed_at
            )
            key = (effective, source.id)
            by_day = days.setdefault(row.vocabulary_id, {})
            existing = by_day.get(row.study_date)
            if existing is None or key > existing[:2]:
                by_day[row.study_date] = (effective, source.id, row)
        record_rows = (
            await self.session.execute(
                select(StudyRecordSnapshot, ApiSnapshot)
                .join(ApiSnapshot, StudyRecordSnapshot.source_snapshot_id == ApiSnapshot.id)
                .join(IngestionRun, ApiSnapshot.ingestion_run_id == IngestionRun.id)
                .where(*visible)
            )
        ).all()
        records: dict[UUID, tuple[datetime, UUID, StudyRecordSnapshot]] = {}
        responses: dict[UUID, dict[tuple[datetime, str], RecentResponse]] = {}
        for record_row, record_source in record_rows:
            effective = (
                record_row.observed_at if record_row.observed_at <= at else record_source.fetched_at
            )
            if record_row.last_studied_at is not None and record_row.last_feedback is not None:
                if record_row.last_studied_at <= at:
                    events = responses.setdefault(record_row.vocabulary_id, {})
                    event_key = (record_row.last_studied_at, record_row.last_feedback)
                    # Earliest retained observation supplies evidence range; polling
                    # refreshes never manufacture a second response event.
                    existing_event = events.get(event_key)
                    first_seen = min(effective, record_source.fetched_at)
                    if existing_event is None or first_seen < existing_event.observed_at:
                        events[event_key] = RecentResponse(
                            record_row.last_feedback, record_row.last_studied_at, first_seen
                        )
            existing_record = records.get(record_row.vocabulary_id)
            if existing_record is None or (effective, record_source.id) > existing_record[:2]:
                records[record_row.vocabulary_id] = (effective, record_source.id, record_row)
        word_ids = set(days) | set(records)
        words = (
            await self.session.scalars(
                select(Vocabulary)
                .where(Vocabulary.id.in_(word_ids), Vocabulary.first_seen_at <= at)
                .order_by(Vocabulary.id)
            )
        ).all()
        results: list[WeaknessEvidence] = []
        for word in words:
            selected_days = days.get(word.id, {})
            observations = tuple(
                DailyEvidence(
                    day,
                    effective,
                    row.first_feedback,
                    row.is_complete,
                    row.is_new,
                    row.first_observed_at,
                )
                for day, (effective, _, row) in sorted(selected_days.items())
            )
            selected_record = records.get(word.id)
            record = selected_record[2] if selected_record else None
            sources = [(effective, source_id) for effective, source_id, _ in selected_days.values()]
            if selected_record:
                sources.append(selected_record[:2])
            results.append(
                WeaknessEvidence(
                    vocabulary_id=word.id,
                    spelling=word.spelling,
                    observations=observations,
                    record_observed_at=selected_record[0] if selected_record else None,
                    last_feedback=record.last_feedback if record else None,
                    last_studied_at=record.last_studied_at if record else None,
                    next_study_at=record.next_study_at if record else None,
                    added_at=record.added_at if record else None,
                    study_count=record.study_count if record else None,
                    tags=tuple(record.tags) if record and record.tags is not None else None,
                    quality=health.completeness,
                    latest_snapshot_id=max(sources)[1],
                    recent_responses=tuple(responses.get(word.id, {}).values()),
                )
            )
        return results

    async def save_weakness(self, result: WeaknessResult) -> None:
        if result.latest_snapshot_id is None:
            raise ValueError("Persisted weakness requires a source snapshot")
        values = {
            "score": result.score,
            "risk_level": result.risk_level,
            "confidence": result.confidence,
            "factors": {
                "values": {
                    name: {"value": factor.value, "weight": factor.weight}
                    for name, factor in result.factors.items()
                },
                "reason_codes": list(result.reason_codes),
                "is_new": result.is_new,
                "spelling": result.spelling,
            },
            "evidence_from": result.evidence_from,
            "evidence_through": result.evidence_through,
            "latest_snapshot_id": result.latest_snapshot_id,
        }
        await self.session.execute(
            insert(WeaknessScore)
            .values(
                id=uuid4(),
                vocabulary_id=result.vocabulary_id,
                algorithm_version=result.algorithm_version,
                computed_at=result.computed_at,
                **values,
            )
            .on_conflict_do_update(constraint="uq_weakness_score_version", set_=values)
        )

    async def weakness_analysis_state(
        self,
        as_of: datetime,
        version: str,
    ) -> WeaknessAnalysisState:
        at = utc_instant(as_of)
        latest = (
            select(
                WeaknessScore.id,
                func.row_number()
                .over(
                    partition_by=WeaknessScore.vocabulary_id,
                    order_by=(WeaknessScore.computed_at.desc(), WeaknessScore.id.desc()),
                )
                .label("position"),
            )
            .where(
                WeaknessScore.algorithm_version == version,
                WeaknessScore.computed_at <= at,
            )
            .subquery()
        )
        rows = (
            await self.session.execute(
                select(
                    WeaknessScore.vocabulary_id,
                    WeaknessScore.computed_at,
                    WeaknessScore.evidence_through,
                    WeaknessScore.factors,
                )
                .join(latest, latest.c.id == WeaknessScore.id)
                .where(latest.c.position == 1)
            )
        ).all()
        scored = {row.vocabulary_id for row in rows}
        observed = {word.vocabulary_id for word in await self.weakness_evidence(at)}
        missing_cutoff = any(row.evidence_through is None for row in rows)
        quality_codes = tuple(
            code
            for code in (
                "DATA_QUALITY_PARTIAL",
                "DATA_QUALITY_STALE",
                "DATA_QUALITY_UNAVAILABLE",
            )
            if any(code in row.factors.get("reason_codes", []) for row in rows)
        )
        return WeaknessAnalysisState(
            score_count=len(rows),
            unscored_word_count=len(observed - scored),
            oldest_computed_at=min((row.computed_at for row in rows), default=None),
            data_through=(
                None
                if missing_cutoff
                else min((row.evidence_through for row in rows), default=None)
            ),
            missing_cutoff=missing_cutoff,
            quality_codes=quality_codes,
        )

    async def weak_words(
        self,
        query: WeakWordQuery,
        version: str,
        *,
        vocabulary_ids: Sequence[UUID] | None = None,
    ) -> list[WeaknessScore]:
        # Select latest first: an old high score must not survive a newer low score.
        latest = (
            select(
                WeaknessScore.id,
                func.row_number()
                .over(
                    partition_by=WeaknessScore.vocabulary_id,
                    order_by=(WeaknessScore.computed_at.desc(), WeaknessScore.id.desc()),
                )
                .label("position"),
            )
            .where(
                WeaknessScore.algorithm_version == version,
                WeaknessScore.computed_at <= query.as_of,
            )
            .subquery()
        )
        statement = (
            select(WeaknessScore)
            .join(latest, latest.c.id == WeaknessScore.id)
            .where(
                latest.c.position == 1,
                WeaknessScore.score >= query.min_score,
            )
        )
        if vocabulary_ids is not None:
            statement = statement.where(WeaknessScore.vocabulary_id.in_(vocabulary_ids))
        if query.start is not None:
            statement = statement.where(WeaknessScore.evidence_through >= query.start)
        if query.end is not None:
            statement = statement.where(WeaknessScore.evidence_from <= query.end)
        return list(
            (
                await self.session.scalars(
                    statement.order_by(
                        WeaknessScore.score.desc(),
                        WeaknessScore.confidence.desc(),
                        WeaknessScore.vocabulary_id,
                    ).limit(query.limit)
                )
            ).all()
        )

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
