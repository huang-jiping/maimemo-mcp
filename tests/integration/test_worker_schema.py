"""Scheduled slots survive upgrades and enforce one successful formal result."""

import asyncio
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine


async def test_slot_column_and_partial_success_uniqueness(database: AsyncEngine) -> None:
    async with database.connect() as connection:
        columns = await connection.run_sync(lambda conn: inspect(conn).get_columns("ingestion_run"))
        assert "scheduled_at" in {column["name"] for column in columns}
    statement = text(
        "INSERT INTO ingestion_run (id, task_type, status, scheduled_at) "
        "VALUES (:id, :task, :status, '2030-10-02T10:00:00+08:00')"
    )
    for status in ("failed", "failed", "complete"):
        async with database.begin() as connection:
            await connection.execute(statement, {"id": uuid4(), "task": "today", "status": status})
    with pytest.raises(IntegrityError):
        async with database.begin() as connection:
            await connection.execute(
                statement,
                {
                    "id": uuid4(),
                    "task": "today",
                    "status": "partial",
                },
            )
    async with database.begin() as connection:
        await connection.execute(
            statement, {"id": uuid4(), "task": "records", "status": "complete"}
        )


async def test_0003_round_trip_preserves_unscheduled_runs(
    database: AsyncEngine,
    alembic_config: Config,
) -> None:
    identity = uuid4()
    async with database.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO ingestion_run (id, task_type, status) "
                "VALUES (:id, 'today', 'complete')"
            ),
            {"id": identity},
        )
    await asyncio.to_thread(command.downgrade, alembic_config, "0002")
    await asyncio.to_thread(command.upgrade, alembic_config, "head")
    async with database.connect() as connection:
        assert await connection.scalar(text("SELECT id FROM ingestion_run")) == identity
        assert await connection.scalar(text("SELECT scheduled_at FROM ingestion_run")) is None
        assert await connection.scalar(text("SELECT schema_version FROM schema_metadata")) == "0003"


async def test_0003_downgrade_rejects_losing_scheduled_identity(
    database: AsyncEngine,
    alembic_config: Config,
) -> None:
    async with database.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO ingestion_run (id, task_type, status, scheduled_at) "
                "VALUES (:id, 'today', 'complete', '2030-10-02T10:00:00+08:00')"
            ),
            {"id": uuid4()},
        )
    with pytest.raises(DataError, match="scheduled"):
        await asyncio.to_thread(command.downgrade, alembic_config, "0002")
    async with database.connect() as connection:
        assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0003"
        assert await connection.scalar(text("SELECT count(*) FROM ingestion_run")) == 1
