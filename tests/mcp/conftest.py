"""Workflow tests use the guarded disposable PostgreSQL and real SDK lifespan."""

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from maimemo_mcp.config import MCPSettings as Settings
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture
async def workflow_database() -> AsyncIterator[AsyncEngine]:
    url = os.environ.get(
        "MAIMEMO_TEST_DATABASE_URL",
        "postgresql+psycopg://maimemo_test:test_only@127.0.0.1:55432/maimemo_test",
    )
    if not (make_url(url).database or "").endswith("_test"):
        raise ValueError("Workflow tests require a disposable database ending in _test")
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_async_engine(url)
    try:
        yield engine
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("TRUNCATE vocabulary, ingestion_run, api_rate_limit_window CASCADE")
            )
        await engine.dispose()
        await asyncio.to_thread(command.downgrade, config, "base")


@pytest.fixture
def workflow_settings(
    tmp_path: Path,
    workflow_database: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> Settings:
    async def forbidden_network(
        transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> None:
        raise AssertionError("Workflow tests must not contact a real upstream")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_network)
    token, key = tmp_path / "token", tmp_path / "key"
    token.write_text("test-token-only", encoding="utf-8")
    key.write_text("test-key-only", encoding="utf-8")
    return Settings.load(
        {
            "MAIMEMO_DATABASE_URL": workflow_database.url.render_as_string(
                hide_password=False
            ),
            "MAIMEMO_TOKEN_FILE": str(token),
            "MAIMEMO_TOKEN_FINGERPRINT_KEY_FILE": str(key),
        }
    )
