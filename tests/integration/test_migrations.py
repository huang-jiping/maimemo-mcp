"""Migration tests catch missing schema, wrong types, and irreversible upgrades."""

import asyncio

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

EXPECTED_TABLES = {
    "vocabulary",
    "ingestion_run",
    "api_snapshot",
    "api_rate_limit_window",
    "daily_progress",
    "daily_word_observation",
    "study_record_snapshot",
    "weakness_score",
    "learning_feedback_event",
    "schema_metadata",
    "alembic_version",
}


async def test_upgrade_creates_expected_tables(database: AsyncEngine) -> None:
    async with database.connect() as connection:
        tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
        assert set(tables) == EXPECTED_TABLES
        version = await connection.scalar(text("SELECT schema_version FROM schema_metadata"))
        assert version == "0001"


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
        assert json_types == ["jsonb"] * 3


async def test_migration_and_orm_have_no_schema_drift(database: AsyncEngine) -> None:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from maimemo_mcp.storage.base import Base

    async with database.connect() as connection:
        differences = await connection.run_sync(
            lambda conn: compare_metadata(MigrationContext.configure(conn), Base.metadata)
        )
        assert differences == []
