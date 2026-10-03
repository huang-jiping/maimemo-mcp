"""Run Alembic using the same psycopg async dialect as the application."""

import asyncio
import os
import sys

from alembic import context
from maimemo.storage import models  # noqa: F401
from maimemo.storage.base import Base
from maimemo.storage.database import create_async_engine
from sqlalchemy import Connection


def database_url() -> str:
    url = context.config.get_main_option("sqlalchemy.url") or os.environ.get("MAIMEMO_DATABASE_URL")
    if not url:
        raise ValueError("Set MAIMEMO_DATABASE_URL to run database migrations")
    return url


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    # Migrations need a database URL only; they never access API secrets.
    engine = create_async_engine(database_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=database_url(),
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        run_migrations(connection)
    elif sys.platform == "win32":
        asyncio.run(run_online(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(run_online())
