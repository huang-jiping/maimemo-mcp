"""Synthetic history in real PostgreSQL catches future leaks and version double counts."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from maimemo_mcp.analysis.models import WeakWordQuery
from maimemo_mcp.analysis.service import WeaknessService
from maimemo_mcp.ingestion.normalizers import (
    DailyWordObservationInput,
    StudyRecordSnapshotInput,
)
from maimemo_mcp.storage.models.analysis import WeaknessScore
from maimemo_mcp.storage.models.ingestion import IngestionRun
from maimemo_mcp.storage.models.learning import Vocabulary
from maimemo_mcp.storage.repositories import StudyHistoryRepository

AT = datetime(2030, 10, 2, 12, tzinfo=UTC)


async def seed(
    factory: async_sessionmaker[AsyncSession],
    at: datetime,
    feedback: str,
    *,
    identity: str = "synthetic",
    complete: bool = True,
    tags: list[str] | None = None,
    finished: datetime | None = None,
    records: bool = False,
    last_study: datetime | None = None,
) -> UUID:
    async with factory() as session, session.begin():
        run = IngestionRun(
            task_type="records" if records else "today",
            started_at=at,
            finished_at=finished or at,
            status="complete",
        )
        session.add(run)
        await session.flush()
        repo = StudyHistoryRepository(session)
        snapshot = await repo.snapshot(
            "records" if records else "today",
            {},
            {
                "feedback": feedback,
                "complete": complete,
                "identity": identity,
                "tags": tags,
                "date": at.date().isoformat() if not records else None,
                "last_study": (last_study or at).isoformat() if records else None,
            },
            at,
            run.id,
        )
        if records:
            await repo.records(
                [
                    StudyRecordSnapshotInput(
                        identity,
                        identity,
                        at,
                        AT - timedelta(days=100),
                        AT - timedelta(days=100),
                        last_study or at,
                        AT + timedelta(days=7),
                        feedback,
                        0,
                        tags or [],
                    )
                ],
                snapshot.id,
            )
        else:
            await repo.today(
                [
                    DailyWordObservationInput(
                        at.date(), identity, identity, feedback, False, complete, at
                    )
                ],
                snapshot.id,
            )
        word = await session.scalar(select(Vocabulary.id).where(Vocabulary.maimemo_id == identity))
        assert word is not None
        return word


async def test_recalculate_persists_explanation_and_is_idempotent(database: AsyncEngine) -> None:
    factory = async_sessionmaker(database)
    word = await seed(factory, AT - timedelta(days=1), "FORGET", complete=False)
    await seed(factory, AT, "FORGET", complete=False)
    await seed(factory, AT, "FORGET", tags=["STICKING"], records=True)
    service = WeaknessService(factory)
    assert await service.recalculate(AT) == 1
    assert await service.recalculate(AT) == 1
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(WeaknessScore)) == 1
        row = await session.scalar(select(WeaknessScore))
        assert row is not None
        assert row.vocabulary_id == word
        assert row.score == 85  # interval is known zero:35+25+15+0+10.
        assert row.confidence == pytest.approx(2 / 7)
        assert row.algorithm_version == "weakness-v1"
        assert row.evidence_from == AT - timedelta(days=1)
        assert row.evidence_through == AT
        assert row.factors["values"]["sticking"]["value"] == 1
        assert "STICKING" in row.factors["reason_codes"]
        assert row.latest_snapshot_id is not None
    result = (await service.list_weak_words(WeakWordQuery(as_of=AT)))[0]
    assert result.vocabulary_id == word
    assert result.reasons
    assert await service.recalculate(AT + timedelta(minutes=1)) == 1
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(WeaknessScore)) == 2


async def test_as_of_selects_latest_effective_record_without_future_commit(
    database: AsyncEngine,
) -> None:
    factory = async_sessionmaker(database)
    await seed(factory, AT - timedelta(hours=2), "FORGET", tags=["STICKING"], records=True)
    await seed(factory, AT - timedelta(hours=1), "FAMILIAR", records=True)
    await seed(factory, AT + timedelta(hours=1), "FORGET", tags=["STICKING"], records=True)
    await seed(
        factory,
        AT,
        "FORGET",
        tags=["STICKING"],
        records=True,
        finished=AT + timedelta(hours=2),
        identity="future-commit",
    )
    service = WeaknessService(factory)
    assert await service.recalculate(AT) == 1
    result = (await service.list_weak_words(WeakWordQuery(as_of=AT)))[0]
    assert result.factors["sticking"].value == 0
    assert 0 < result.factors["recent_response"].value < 1
    assert result.evidence_through == AT - timedelta(hours=1)
    assert result.evidence_from == AT - timedelta(hours=2)


async def test_same_day_versions_and_repeated_snapshot_are_not_multiple_errors(
    database: AsyncEngine,
) -> None:
    factory = async_sessionmaker(database)
    await seed(factory, AT - timedelta(days=1), "FORGET")
    await seed(factory, AT - timedelta(days=1) + timedelta(minutes=30), "FORGET")
    await seed(factory, AT - timedelta(days=1) + timedelta(hours=1), "FAMILIAR")
    await seed(factory, AT, "FORGET")
    service = WeaknessService(factory)
    await service.recalculate(AT)
    result = (await service.list_weak_words(WeakWordQuery(as_of=AT)))[0]
    assert result.factors["repeated_error"].value == 0.5
    # Missing records health flow makes quality partial: .7*(2/7)*.5=.1.
    assert result.confidence == pytest.approx(0.1)
    assert result.score == pytest.approx(67.85714285714286)  # (35+12.5)/70*100.


async def test_query_uses_latest_score_then_threshold_range_and_stable_limit(
    database: AsyncEngine,
) -> None:
    factory = async_sessionmaker(database)
    ids = [await seed(factory, AT, "FORGET", identity=name) for name in ("a", "b")]
    service = WeaknessService(factory)
    await service.recalculate(AT)
    query = WeakWordQuery(as_of=AT, min_score=99, start=AT, end=AT, limit=1)
    result = await service.list_weak_words(query)
    assert [row.vocabulary_id for row in result] == [min(ids)]
    assert (
        await service.list_weak_words(WeakWordQuery(as_of=AT, start=AT + timedelta(seconds=1)))
        == []
    )
    assert await service.list_weak_words(WeakWordQuery(as_of=AT - timedelta(seconds=1))) == []
    # The later score supersedes high risk; filtering high scores before latest is a bug.
    await service.recalculate(AT + timedelta(days=14))
    assert (
        await service.list_weak_words(WeakWordQuery(as_of=AT + timedelta(days=14), min_score=99))
        == []
    )


async def test_cutoff_recovers_first_snapshot_time_after_later_duplicate_refresh(
    database: AsyncEngine,
) -> None:
    factory = async_sessionmaker(database)
    await seed(factory, AT - timedelta(hours=2), "FORGET", tags=["STICKING"], records=True)
    await seed(factory, AT - timedelta(hours=1), "FAMILIAR", tags=[], records=True)
    await seed(
        factory, AT + timedelta(hours=1), "FORGET", tags=["STICKING"],
        records=True, last_study=AT - timedelta(hours=2)
    )
    service = WeaknessService(factory)
    await service.recalculate(AT)
    result = (await service.list_weak_words(WeakWordQuery(as_of=AT)))[0]
    assert result.factors["recent_response"].value > 0
    assert result.factors["sticking"].value == 0
    assert result.evidence_through == AT - timedelta(hours=1)
    await service.recalculate(AT + timedelta(hours=1))
    latest = (await service.list_weak_words(WeakWordQuery(as_of=AT + timedelta(hours=1))))[0]
    assert latest.score > 0
    assert latest.factors["sticking"].value == 1
    assert latest.latest_snapshot_id != result.latest_snapshot_id
    assert latest.evidence_through == AT + timedelta(hours=1)


async def test_transaction_failure_rolls_back_all_scores(database: AsyncEngine) -> None:
    factory = async_sessionmaker(database)
    ids = [await seed(factory, AT, "FORGET", identity=name) for name in ("a", "b")]
    service = WeaknessService(factory)
    # Real PG rejects the second word after the first INSERT. No mock persistence.
    rejected = str(max(ids))
    async with database.begin() as connection:
        await connection.execute(
            text(
                "ALTER TABLE weakness_score ADD CONSTRAINT synthetic_failure "
                f"CHECK (vocabulary_id <> '{rejected}'::uuid)"
            )
        )
    try:
        with pytest.raises(IntegrityError):
            await service.recalculate(AT)
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(WeaknessScore)) == 0
    finally:
        async with database.begin() as connection:
            await connection.execute(
                text("ALTER TABLE weakness_score DROP CONSTRAINT synthetic_failure")
            )


async def test_no_visible_history_does_not_create_scores(database: AsyncEngine) -> None:
    factory = async_sessionmaker(database)
    async with factory() as session, session.begin():
        session.add(
            Vocabulary(
                id=uuid4(),
                maimemo_id="unobserved",
                spelling="unobserved",
                normalized_spelling="unobserved",
                first_seen_at=AT,
                last_seen_at=AT,
            )
        )
    assert await WeaknessService(factory).recalculate(AT) == 0


async def test_record_only_familiar_does_not_instantly_erase_old_forget(
    database: AsyncEngine,
) -> None:
    factory = async_sessionmaker(database)
    await seed(factory, AT - timedelta(days=7), "FORGET", records=True)
    await seed(factory, AT, "FAMILIAR", records=True)
    service = WeaknessService(factory)
    await service.recalculate(AT)
    result = (await service.list_weak_words(WeakWordQuery(as_of=AT)))[0]
    assert result.score == pytest.approx(26.923076923076923)
    assert "repeated_error" not in result.factors
    assert result.evidence_from == AT - timedelta(days=7)


async def test_daily_duplicate_keeps_first_feedback_evidence_range(database: AsyncEngine) -> None:
    factory = async_sessionmaker(database)
    await seed(factory, AT - timedelta(hours=1), "FORGET")
    await seed(factory, AT, "FORGET")
    service = WeaknessService(factory)
    await service.recalculate(AT)
    result = (await service.list_weak_words(WeakWordQuery(as_of=AT)))[0]
    assert result.evidence_from == AT - timedelta(hours=1)
    assert result.evidence_through == AT
    assert "repeated_error" not in result.factors
