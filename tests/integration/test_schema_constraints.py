"""Exercise persistence contracts against actual migrated PostgreSQL tables."""

from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine


async def test_snapshot_content_hash_is_unique_per_endpoint_and_request(
    database: AsyncEngine,
) -> None:
    async with database.begin() as connection:
        run = uuid4()
        await connection.execute(
            text(
                "INSERT INTO ingestion_run (id, task_type, status) VALUES (:id, 'today', 'running')"
            ),
            {"id": run},
        )
        query = text(
            "INSERT INTO api_snapshot (id, endpoint, request_hash, content_hash, raw_response, "
            "ingestion_run_id) VALUES (:id, :endpoint, :request, 'abc', '{}'::jsonb, :run)"
        )
        for endpoint, request in [("today", "req1"), ("records", "req1"), ("today", "req2")]:
            await connection.execute(
                query,
                {
                    "id": uuid4(),
                    "endpoint": endpoint,
                    "request": request,
                    "run": run,
                },
            )
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(
                    query,
                    {
                        "id": uuid4(),
                        "endpoint": "today",
                        "request": "req1",
                        "run": run,
                    },
                )


async def test_feedback_retraction_references_existing_event(database: AsyncEngine) -> None:
    async with database.begin() as connection:
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(
                    text(
                        "INSERT INTO learning_feedback_event (id, event_type, idempotency_key, "
                        "retracted_event_id) VALUES (:id, 'RETRACTION', 'missing', :target)"
                    ),
                    {"id": uuid4(), "target": uuid4()},
                )
        event = uuid4()
        await connection.execute(
            text(
                "INSERT INTO learning_feedback_event (id, event_type, word_a_spelling, "
                "word_b_spelling, relation_type, direction, evidence_type, source_agent, "
                "idempotency_key) VALUES (:id, 'CONFUSION', 'affect', 'effect', 'CONFUSED_WITH', "
                "'A_TO_B', 'USER_CONFIRMED', 'test', 'original')"
            ),
            {"id": event},
        )
        await connection.execute(
            text(
                "INSERT INTO learning_feedback_event (id, event_type, idempotency_key, "
                "retracted_event_id) VALUES (:id, 'RETRACTION', 'retract', :target)"
            ),
            {"id": uuid4(), "target": event},
        )


async def test_invalid_local_states_and_negative_counts_are_rejected(database: AsyncEngine) -> None:
    async with database.begin() as connection:
        for status, count in [("unknown", 0), ("running", -1)]:
            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            "INSERT INTO ingestion_run (id, task_type, status, request_count) "
                            "VALUES (:id, 'today', :status, :count)"
                        ),
                        {"id": uuid4(), "status": status, "count": count},
                    )


async def test_session_factory_persists_and_reads_timezone_aware_data(
    postgres_url: str,
    database: AsyncEngine,
) -> None:
    from maimemo_mcp.config import Settings
    from maimemo_mcp.storage.database import (
        create_async_engine_from_settings,
        create_session_factory,
    )
    from maimemo_mcp.storage.models.learning import Vocabulary

    engine = create_async_engine_from_settings(
        Settings(
            database_url=postgres_url,
            token_file=Path("unused"),
            token_fingerprint_key_file=Path("unused"),
        )
    )
    try:
        factory = create_session_factory(engine)
        async with factory.begin() as session:
            word = Vocabulary(maimemo_id=123, normalized_spelling="affect", spelling="affect")
            session.add(word)
        async with factory() as session:
            stored = await session.get(Vocabulary, word.id)
            assert stored is not None
            assert stored.maimemo_id == 123
            assert stored.first_seen_at.utcoffset() is not None
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "psycopg"
    finally:
        await engine.dispose()


async def test_unknown_upstream_states_and_missing_tags_remain_representable(
    database: AsyncEngine,
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from maimemo_mcp.storage.models.ingestion import ApiSnapshot, IngestionRun
    from maimemo_mcp.storage.models.learning import (
        DailyWordObservation,
        StudyRecordSnapshot,
        Vocabulary,
    )

    factory = async_sessionmaker(database, expire_on_commit=False)
    async with factory.begin() as session:
        word = Vocabulary(maimemo_id=456, normalized_spelling="test", spelling="test")
        run = IngestionRun(task_type="today", status="running")
        session.add_all([word, run])
        await session.flush()
        snapshot = ApiSnapshot(
            endpoint="today",
            request_hash="req",
            content_hash="content",
            raw_response={},
            ingestion_run_id=run.id,
        )
        session.add(snapshot)
        await session.flush()
        record = StudyRecordSnapshot(
            vocabulary_id=word.id,
            source_snapshot_id=snapshot.id,
            last_feedback="FUTURE_FEEDBACK",
            tags=None,
        )
        session.add(record)
        session.add(
            DailyWordObservation(
                study_date=date(2026, 10, 2),
                vocabulary_id=word.id,
                source_snapshot_id=snapshot.id,
                first_feedback="FUTURE_FEEDBACK",
                is_new=None,
                is_complete=None,
            )
        )
    async with factory() as session:
        stored = await session.get(StudyRecordSnapshot, record.id)
        assert stored is not None
        assert stored.last_feedback == "FUTURE_FEEDBACK"
        assert stored.tags is None


@pytest.mark.parametrize(
    "table, columns, values",
    [
        ("vocabulary", "maimemo_id, normalized_spelling, spelling", "789, 'one', 'One'"),
        ("daily_progress", "study_date, completeness", "'2026-10-02', 'partial'"),
        (
            "api_rate_limit_window",
            "token_hash, window_type, window_start",
            "repeat('a', 64), '10s', '2026-10-02T00:00:00Z'",
        ),
        (
            "learning_feedback_event",
            "event_type, word_a_spelling, word_b_spelling, relation_type, direction, "
            "evidence_type, "
            "source_agent, idempotency_key",
            "'CONFUSION', 'a', 'b', 'CONFUSED_WITH', 'A_TO_B', 'QUIZ_OBSERVED', 'test', 'same-key'",
        ),
    ],
)
async def test_domain_identity_rejects_duplicate_rows(
    database: AsyncEngine,
    table: str,
    columns: str,
    values: str,
) -> None:
    # Query fragments are literal test cases, never external input.
    query = text(f"INSERT INTO {table} (id, {columns}) VALUES (:id, {values})")
    async with database.begin() as connection:
        await connection.execute(query, {"id": uuid4()})
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(query, {"id": uuid4()})


async def test_feedback_does_not_accept_a_fabricated_resolved_word(database: AsyncEngine) -> None:
    async with database.begin() as connection:
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(
                    text(
                        "INSERT INTO learning_feedback_event "
                        "(id, event_type, word_a_id, word_a_status, "
                        "word_b_spelling, relation_type, direction, evidence_type, source_agent, "
                        "idempotency_key) VALUES (:id, 'CONFUSION', :word, 'RESOLVED', 'effect', "
                        "'CONFUSED_WITH', 'A_TO_B', 'USER_CONFIRMED', 'test', 'fake')"
                    ),
                    {"id": uuid4(), "word": uuid4()},
                )


async def test_partial_progress_still_rejects_negative_total(database: AsyncEngine) -> None:
    async with database.begin() as connection:
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(
                    text(
                        "INSERT INTO daily_progress (id, study_date, completeness, total_count) "
                        "VALUES (:id, '2026-10-02', 'partial', -1)"
                    ),
                    {"id": uuid4()},
                )
