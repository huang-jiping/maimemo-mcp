"""Real PostgreSQL fixtures; use only the disposable test database."""

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture
def postgres_url() -> str:
    url = os.environ.get(
        "MAIMEMO_TEST_DATABASE_URL",
        "postgresql+psycopg://maimemo_test:test_only@127.0.0.1:55432/maimemo_test",
    )
    if not (make_url(url).database or "").endswith("_test"):
        raise ValueError(
            "Migration tests require a disposable database with a name ending in _test"
        )
    return url


@pytest.fixture
def alembic_config(postgres_url: str) -> Config:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", postgres_url.replace("%", "%%"))
    return config


@pytest.fixture
async def database(postgres_url: str, alembic_config: Config) -> AsyncIterator[AsyncEngine]:
    await asyncio.to_thread(command.upgrade, alembic_config, "head")
    engine = create_async_engine(postgres_url)
    try:
        yield engine
    finally:
        await engine.dispose()
        await asyncio.to_thread(command.downgrade, alembic_config, "base")
