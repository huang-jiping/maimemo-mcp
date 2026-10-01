"""Session-bound persistence; callers own commit/rollback and formal write authority."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maimemo_mcp.ingestion.hashing import stable_payload_hash
from maimemo_mcp.ingestion.normalizers import (
    DailyProgressInput,
    DailyWordObservationInput,
    StudyRecordSnapshotInput,
)
from maimemo_mcp.storage.models.ingestion import ApiSnapshot
from maimemo_mcp.storage.models.learning import (
    DailyProgress,
    DailyWordObservation,
    StudyRecordSnapshot,
    Vocabulary,
)


class StudyHistoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

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
