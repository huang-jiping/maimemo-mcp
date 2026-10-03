"""Real PostgreSQL tests: append-only events, conflicts, retractions and resolution."""

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from maimemo.api_client.models import Vocabulary as UpstreamVocabulary
from maimemo.feedback.models import (
    EvidenceType,
    FeedbackDirection,
    FeedbackEventView,
    FeedbackQuery,
    FeedbackRelationType,
    RecordFeedbackCommand,
    RetractFeedbackCommand,
)
from maimemo.feedback.service import FeedbackConflictError, FeedbackService
from maimemo.storage.models import LearningFeedbackEvent, Vocabulary
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def command(key: str = "record-key", /, **changes: object) -> RecordFeedbackCommand:
    values: dict[str, object] = {
        "word_a_spelling": " Affect ",
        "word_b_spelling": "Effect",
        "relation_type": "FORM_SIMILAR",
        "direction": "A_TO_B",
        "evidence_type": "USER_CONFIRMED",
        "explicit_confirmation": True,
        "source_agent": "synthetic-agent",
        "idempotency_key": key,
    }
    return RecordFeedbackCommand.model_validate(values | changes)


def service(database: AsyncEngine) -> FeedbackService:
    return FeedbackService(async_sessionmaker(database, expire_on_commit=False))


async def test_same_idempotency_key_returns_existing_event(database: AsyncEngine) -> None:
    feedback = service(database)
    first = await feedback.record(command())
    assert await feedback.record(command()) == first
    async with AsyncSession(database) as session:
        assert await session.scalar(select(func.count()).select_from(LearningFeedbackEvent)) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"direction": "B_TO_A"},
        {"note": "different note"},
        {"source_agent": "other-agent"},
        {"session_reference": "other-session"},
        {"word_a_spelling": "affect"},
        {"relation_type": "SOUND_SIMILAR"},
        {"evidence_type": "QUIZ_OBSERVED", "explicit_confirmation": False},
    ],
)
async def test_idempotency_payload_conflict_is_not_silent(
    database: AsyncEngine, changes: dict[str, object]
) -> None:
    feedback = service(database)
    await feedback.record(command())
    with pytest.raises(FeedbackConflictError):
        await feedback.record(command(**changes))


async def test_retract_appends_event_and_keeps_original(database: AsyncEngine) -> None:
    feedback = service(database)
    original = await feedback.record(command())
    retract = RetractFeedbackCommand(
        event_id=original.id, idempotency_key="retract-key", source_agent="synthetic-agent"
    )
    result = await feedback.retract(retract)
    assert result.event_type == "RETRACTION"
    assert result.retracted_event_id == original.id
    assert await feedback.retract(retract) == result
    assert await feedback.list_active(FeedbackQuery()) == []
    async with AsyncSession(database) as session:
        assert await session.scalar(select(func.count()).select_from(LearningFeedbackEvent)) == 2
        retained = await session.get(LearningFeedbackEvent, original.id)
        assert retained is not None
        assert retained.event_type == "CONFUSION"
        assert retained.retracted_event_id is None
        assert retained.word_a_spelling == " Affect "
        assert retained.direction == "A_TO_B"
        assert retained.evidence_type == "USER_CONFIRMED"
        assert FeedbackEventView.model_validate(retained) == original


async def test_cannot_retract_twice_or_retract_a_retraction(database: AsyncEngine) -> None:
    feedback = service(database)
    original = await feedback.record(command())
    result = await feedback.retract(
        RetractFeedbackCommand(
            event_id=original.id, idempotency_key="retract-key", source_agent="synthetic-agent"
        )
    )
    for target in (original.id, result.id, uuid4()):
        with pytest.raises(FeedbackConflictError):
            await feedback.retract(
                RetractFeedbackCommand(
                    event_id=target, idempotency_key=str(target), source_agent="synthetic-agent"
                )
            )


async def test_retraction_idempotency_checks_payload_and_event_type(database: AsyncEngine) -> None:
    feedback = service(database)
    original = await feedback.record(command())
    other = await feedback.record(command("other-key"))
    retract = RetractFeedbackCommand(
        event_id=original.id, idempotency_key="retract-key", source_agent="synthetic-agent"
    )
    await feedback.retract(retract)
    for changes in ({"event_id": other.id}, {"note": "changed"}, {"source_agent": "other"}):
        with pytest.raises(FeedbackConflictError):
            await feedback.retract(
                RetractFeedbackCommand.model_validate(retract.model_dump() | changes)
            )
    with pytest.raises(FeedbackConflictError):
        await feedback.record(command("retract-key"))
    with pytest.raises(FeedbackConflictError):
        await feedback.retract(retract.model_copy(update={"idempotency_key": "record-key"}))


async def test_unresolved_spelling_is_saved_without_fake_vocabulary_id(
    database: AsyncEngine,
) -> None:
    result = await service(database).record(command())
    assert result.word_a_status == "UNRESOLVED"
    assert result.word_b_status == "UNRESOLVED"
    assert result.word_a_id is None
    assert result.word_b_id is None
    assert result.word_a_spelling == " Affect "
    assert result.direction == "A_TO_B"


async def test_local_normalized_resolution_preserves_original_spelling(
    database: AsyncEngine,
) -> None:
    async with AsyncSession(database) as session, session.begin():
        word = Vocabulary(
            maimemo_id="synthetic-affect", normalized_spelling="affect", spelling="affect"
        )
        session.add(word)
        await session.flush()
        identity = word.id
    result = await service(database).record(command())
    assert result.word_a_id == identity
    assert result.word_a_status == "RESOLVED"
    assert result.word_a_spelling == " Affect "


async def test_optional_resolver_failure_does_not_block_feedback(database: AsyncEngine) -> None:
    async def failed(spelling: str) -> UpstreamVocabulary | None:
        raise RuntimeError("synthetic unavailable resolver")

    feedback = FeedbackService(
        async_sessionmaker(database, expire_on_commit=False), resolver=failed
    )
    result = await feedback.record(command())
    assert result.word_a_id is None
    assert result.word_b_status == "UNRESOLVED"


async def test_verified_resolver_id_is_saved_and_mismatch_is_unresolved(
    database: AsyncEngine,
) -> None:
    async def resolve(spelling: str) -> UpstreamVocabulary | None:
        return UpstreamVocabulary(id="synthetic-affect", spelling="affect")

    feedback = FeedbackService(
        async_sessionmaker(database, expire_on_commit=False), resolver=resolve
    )
    result = await feedback.record(command())
    assert result.word_a_status == "RESOLVED"
    assert result.word_a_id is not None
    assert result.word_b_id is None
    async with AsyncSession(database) as session:
        word = await session.get(Vocabulary, result.word_a_id)
        assert word is not None
        assert word.maimemo_id == "synthetic-affect"


async def test_quiz_observed_cannot_be_returned_as_user_confirmed(database: AsyncEngine) -> None:
    feedback = service(database)
    quiz = await feedback.record(
        command(evidence_type="QUIZ_OBSERVED", explicit_confirmation=False)
    )
    assert quiz.evidence_type == "QUIZ_OBSERVED"
    assert (
        await feedback.list_active(FeedbackQuery(evidence_type=EvidenceType.USER_CONFIRMED)) == []
    )
    assert await feedback.list_active(FeedbackQuery(evidence_type=EvidenceType.QUIZ_OBSERVED)) == [
        quiz
    ]


async def test_active_filters_direction_relation_word_and_stable_order(
    database: AsyncEngine,
) -> None:
    feedback = service(database)
    first = await feedback.record(command())
    second = await feedback.record(
        command("second", direction="B_TO_A", relation_type="SOUND_SIMILAR")
    )
    async with database.begin() as connection:
        await connection.execute(
            text("UPDATE learning_feedback_event SET created_at = '2026-10-02Z'")
        )
    all_rows = await feedback.list_active(FeedbackQuery(word_spelling=" AFFECT "))
    assert [row.id for row in all_rows] == sorted([first.id, second.id])
    assert len(await feedback.list_active(FeedbackQuery(direction=FeedbackDirection.A_TO_B))) == 1
    assert (
        len(
            await feedback.list_active(
                FeedbackQuery(relation_type=FeedbackRelationType.SOUND_SIMILAR)
            )
        )
        == 1
    )
    assert await feedback.list_active(FeedbackQuery(word_spelling="unknown")) == []
    assert len(await feedback.list_active(FeedbackQuery(limit=1))) == 1


async def test_concurrent_record_and_retraction_remain_single_events(database: AsyncEngine) -> None:
    feedback = service(database)
    records = await asyncio.gather(*(feedback.record(command()) for _ in range(4)))
    assert len({row.id for row in records}) == 1
    retract = RetractFeedbackCommand(
        event_id=records[0].id, idempotency_key="retract-key", source_agent="synthetic-agent"
    )
    retractions = await asyncio.gather(*(feedback.retract(retract) for _ in range(4)))
    assert len({row.id for row in retractions}) == 1
    async with AsyncSession(database) as session:
        assert await session.scalar(select(func.count()).select_from(LearningFeedbackEvent)) == 2


async def test_concurrent_different_retract_keys_allow_one_only(database: AsyncEngine) -> None:
    feedback = service(database)
    original = await feedback.record(command())
    results = await asyncio.gather(
        *(
            feedback.retract(
                RetractFeedbackCommand(
                    event_id=original.id, idempotency_key=f"retract-{index}", source_agent="test"
                )
            )
            for index in range(4)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, FeedbackConflictError) for result in results) == 3


async def test_record_and_retract_issue_no_feedback_update_or_delete(database: AsyncEngine) -> None:
    statements: list[str] = []

    def observe(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        statements.append(statement.strip().upper())

    event.listen(database.sync_engine, "before_cursor_execute", observe)
    try:
        feedback = service(database)
        original = await feedback.record(command())
        await feedback.retract(
            RetractFeedbackCommand(
                event_id=original.id, idempotency_key="retraction", source_agent="test"
            )
        )
    finally:
        event.remove(database.sync_engine, "before_cursor_execute", observe)
    assert (
        sum(statement.startswith("INSERT INTO LEARNING_FEEDBACK_EVENT") for statement in statements)
        == 2
    )
    assert not any(statement.startswith(("UPDATE", "DELETE")) for statement in statements)


async def test_local_resolution_query_failure_still_records_unresolved(
    database: AsyncEngine,
) -> None:
    def fail_word_lookup(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> tuple[str, Any]:
        if statement.startswith("SELECT vocabulary.id"):
            return "SELECT 1 / 0", {}
        return statement, parameters

    # Inject an actual PostgreSQL statement failure, preserving transaction behavior.
    event.listen(database.sync_engine, "before_cursor_execute", fail_word_lookup, retval=True)
    try:
        result = await service(database).record(command())
    finally:
        event.remove(database.sync_engine, "before_cursor_execute", fail_word_lookup)
    assert result.word_a_status == "UNRESOLVED"
    assert result.word_b_id is None
    async with AsyncSession(database) as session:
        assert await session.get(LearningFeedbackEvent, result.id) is not None


async def test_resolver_miss_does_not_create_a_vocabulary_identity(database: AsyncEngine) -> None:
    async def missed(spelling: str) -> UpstreamVocabulary | None:
        return None

    feedback = FeedbackService(
        async_sessionmaker(database, expire_on_commit=False), resolver=missed
    )
    result = await feedback.record(command())
    assert result.word_a_id is None
    async with AsyncSession(database) as session:
        assert await session.scalar(select(func.count()).select_from(Vocabulary)) == 0


async def test_ambiguous_local_word_remains_unresolved(database: AsyncEngine) -> None:
    async with AsyncSession(database) as session, session.begin():
        session.add_all(
            [
                Vocabulary(
                    maimemo_id=f"synthetic-{index}", spelling="affect", normalized_spelling="affect"
                )
                for index in range(2)
            ]
        )
    result = await service(database).record(command())
    assert result.word_a_id is None


async def test_unicode_casefold_word_filter_matches_preserved_spelling(
    database: AsyncEngine,
) -> None:
    feedback = service(database)
    original = await feedback.record(command(word_a_spelling=" Straße "))
    assert await feedback.list_active(FeedbackQuery(word_spelling="STRASSE")) == [original]
