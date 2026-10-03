"""Migration tests catch missing schema, wrong types, and irreversible upgrades."""

import asyncio
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import DataError
from sqlalchemy.ext.asyncio import AsyncEngine

EXPECTED_TABLES = {
    "vocabulary",
    "ingestion_run",
    "api_snapshot",
    "failed_api_snapshot",
    "api_rate_limit_window",
    "daily_progress",
    "daily_word_observation",
    "study_record_snapshot",
    "weakness_score",
    "learning_feedback_event",
    "schema_metadata",
    "alembic_version",
}


async def test_server_readiness_confirms_database_and_migration_head(
    postgres_url: str, database: AsyncEngine
) -> None:
    import httpx
    from maimemo.config import DatabaseSettings
    from maimemo_server.app import create_app
    from maimemo_server.config import ServerSettings

    settings = ServerSettings(database=DatabaseSettings(database_url=postgres_url))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)),
        base_url="http://server.test",
    ) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_restricted_migration_entrypoint_runs_current_and_upgrade_head(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from maimemo_server.migrate import main

    monkeypatch.setenv("MAIMEMO_DATABASE_URL", postgres_url)
    assert main(["current"]) == 0
    assert main(["upgrade", "head"]) == 0


async def test_upgrade_creates_expected_tables(database: AsyncEngine) -> None:
    async with database.connect() as connection:
        tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
        assert set(tables) == EXPECTED_TABLES
        version = await connection.scalar(text("SELECT schema_version FROM schema_metadata"))
        assert version == "0004"


async def test_upgrade_downgrade_reupgrade(postgres_url: str, alembic_config: Config) -> None:
    await asyncio.to_thread(command.upgrade, alembic_config, "head")
    await asyncio.to_thread(command.downgrade, alembic_config, "base")
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_url)
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
            assert set(tables) <= {"alembic_version"}
        await asyncio.to_thread(command.upgrade, alembic_config, "head")
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
            assert set(tables) == EXPECTED_TABLES
    finally:
        await engine.dispose()
        await asyncio.to_thread(command.downgrade, alembic_config, "base")


async def test_timestamps_are_timezone_aware(database: AsyncEngine) -> None:
    async with database.connect() as connection:
        types = (
            await connection.execute(
                text(
                    "SELECT table_name, column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND data_type LIKE 'timestamp%'"
                )
            )
        ).all()
        assert types
        assert all(row.data_type == "timestamp with time zone" for row in types)
        json_types: list[str] = list(
            (
                await connection.execute(
                    text(
                        "SELECT data_type FROM information_schema.columns "
                        "WHERE table_schema = 'public' "
                        "AND column_name IN ('raw_response', 'tags', 'factors')"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert json_types == ["jsonb"] * 4


async def test_migration_and_orm_have_no_schema_drift(database: AsyncEngine) -> None:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from maimemo.storage.base import Base

    async with database.connect() as connection:
        differences = await connection.run_sync(
            lambda conn: compare_metadata(MigrationContext.configure(conn), Base.metadata)
        )
        assert differences == []


@pytest.mark.parametrize("with_observation", [False, True])
async def test_0002_round_trip_preserves_compatible_vocabulary(
    database: AsyncEngine,
    alembic_config: Config,
    with_observation: bool,
) -> None:
    identity = uuid4()
    async with database.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO vocabulary (id, maimemo_id, normalized_spelling, spelling) "
                "VALUES (:id, '123', 'apple', 'Apple')"
            ),
            {"id": identity},
        )
        if with_observation:
            run_id = uuid4()
            await connection.execute(
                text(
                    "INSERT INTO ingestion_run (id, task_type, status) "
                    "VALUES (:id, 'today', 'complete')"
                ),
                {"id": run_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO api_snapshot (id, endpoint, request_hash, content_hash, "
                    "raw_response, ingestion_run_id, observation_kind) "
                    "VALUES (:id, 'today', 'request', 'content', '{\"preserved\": true}'::jsonb, "
                    ":run, 'OBSERVATION')"
                ),
                {"id": uuid4(), "run": run_id},
            )
    await asyncio.to_thread(command.downgrade, alembic_config, "0001")
    async with database.connect() as connection:
        assert await connection.scalar(text("SELECT maimemo_id FROM vocabulary")) == 123
    await asyncio.to_thread(command.upgrade, alembic_config, "head")
    async with database.connect() as connection:
        assert await connection.scalar(text("SELECT maimemo_id FROM vocabulary")) == "123"
        assert await connection.scalar(text("SELECT id FROM vocabulary")) == identity
        if with_observation:
            assert await connection.scalar(text("SELECT observation_kind FROM api_snapshot")) == (
                "OBSERVATION"
            )
            assert await connection.scalar(text("SELECT raw_response FROM api_snapshot")) == {
                "preserved": True,
            }


@pytest.mark.parametrize("upstream_id", ["opaque", "007"])
async def test_0002_downgrade_rejects_incompatible_ids_without_deleting_data(
    database: AsyncEngine,
    alembic_config: Config,
    upstream_id: str,
) -> None:
    async with database.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO vocabulary (id, maimemo_id, normalized_spelling, spelling) "
                "VALUES (:id, :upstream_id, 'apple', 'Apple')"
            ),
            {"id": uuid4(), "upstream_id": upstream_id},
        )
    with pytest.raises(DataError):
        await asyncio.to_thread(command.downgrade, alembic_config, "0001")
    async with database.connect() as connection:
        assert await connection.scalar(text("SELECT maimemo_id FROM vocabulary")) == upstream_id
        assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0004"


async def test_0002_downgrade_rejects_baseline_without_losing_provenance(
    database: AsyncEngine,
    alembic_config: Config,
) -> None:
    run_id, snapshot_id, word_id = uuid4(), uuid4(), uuid4()
    async with database.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO vocabulary (id, maimemo_id, normalized_spelling, spelling) "
                "VALUES (:id, '123', 'apple', 'Apple')"
            ),
            {"id": word_id},
        )
        await connection.execute(
            text(
                "INSERT INTO ingestion_run (id, task_type, status) "
                "VALUES (:id, 'today', 'complete')"
            ),
            {"id": run_id},
        )
        await connection.execute(
            text(
                "INSERT INTO api_snapshot (id, endpoint, request_hash, content_hash, "
                "raw_response, ingestion_run_id, observation_kind) "
                "VALUES (:id, 'today', 'request', 'content', '{}'::jsonb, :run, 'BASELINE')"
            ),
            {"id": snapshot_id, "run": run_id},
        )
        await connection.execute(
            text(
                "INSERT INTO daily_word_observation "
                "(id, study_date, vocabulary_id, source_snapshot_id) "
                "VALUES (:id, '2026-10-02', :word, :snapshot)"
            ),
            {"id": uuid4(), "word": word_id, "snapshot": snapshot_id},
        )
    with pytest.raises(DataError):
        await asyncio.to_thread(command.downgrade, alembic_config, "0001")
    async with database.connect() as connection:
        assert (
            await connection.scalar(text("SELECT observation_kind FROM api_snapshot")) == "BASELINE"
        )
        assert await connection.scalar(text("SELECT count(*) FROM daily_word_observation")) == 1
        assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0004"
