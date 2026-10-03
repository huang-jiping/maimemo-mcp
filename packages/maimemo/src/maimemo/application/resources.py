"""Fine-grained ownership of database and upstream API resources."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from maimemo.api_client.markji import MarkjiClient
from maimemo.api_client.memo_content import MemoContentClient
from maimemo.api_client.rate_limit import SharedRateLimiter
from maimemo.api_client.study import StudyClient
from maimemo.api_client.transport import MaimemoTransport
from maimemo.config import DatabaseSettings, UpstreamCredentialSettings
from maimemo.storage.database import create_async_engine, create_session_factory


@dataclass(frozen=True, repr=False)
class DatabaseResources:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]


@dataclass(frozen=True, repr=False)
class UpstreamResources:
    http_client: httpx.AsyncClient
    limiter: SharedRateLimiter
    transport: MaimemoTransport
    markji: MarkjiClient
    memo_content: MemoContentClient
    study: StudyClient


@asynccontextmanager
async def open_database(database: DatabaseSettings) -> AsyncIterator[DatabaseResources]:
    engine = create_async_engine(database.database_url)
    try:
        yield DatabaseResources(engine=engine, sessions=create_session_factory(engine))
    finally:
        await engine.dispose()


@asynccontextmanager
async def open_upstream(
    credentials: UpstreamCredentialSettings,
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[UpstreamResources]:
    http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
    limiter = SharedRateLimiter(sessions)
    transport = MaimemoTransport(
        credentials.read_maimemo_token(),
        credentials.read_token_fingerprint_key(),
        limiter,
        client=http_client,
    )
    try:
        yield UpstreamResources(
            http_client=http_client,
            limiter=limiter,
            transport=transport,
            markji=MarkjiClient(transport),
            memo_content=MemoContentClient(transport),
            study=StudyClient(transport),
        )
    finally:
        await transport.aclose()
        await http_client.aclose()
