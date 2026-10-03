"""One MCP lifespan owns all database and upstream resources."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

import anyio
import httpx
from maimemo.analysis.service import WeaknessService
from maimemo.api_client.markji import MarkjiClient
from maimemo.api_client.memo_content import MemoContentClient
from maimemo.api_client.rate_limit import SharedRateLimiter
from maimemo.api_client.study import StudyClient
from maimemo.api_client.transport import MaimemoTransport
from maimemo.application import open_database, open_upstream
from maimemo.feedback.service import FeedbackService
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from maimemo_mcp.config import MCPSettings


@dataclass(frozen=True, repr=False)
class MCPDependencies:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    http_client: httpx.AsyncClient
    limiter: SharedRateLimiter
    transport: MaimemoTransport
    markji: MarkjiClient
    memo_content: MemoContentClient
    study: StudyClient
    feedback: FeedbackService
    weakness: WeaknessService


@asynccontextmanager
async def open_mcp_dependencies(settings: MCPSettings) -> AsyncIterator[MCPDependencies]:
    stack = AsyncExitStack()
    try:
        database = await stack.enter_async_context(open_database(settings.core.database))
        upstream = await stack.enter_async_context(
            open_upstream(settings.upstream, database.sessions)
        )
        yield MCPDependencies(
            engine=database.engine,
            sessions=database.sessions,
            http_client=upstream.http_client,
            limiter=upstream.limiter,
            transport=upstream.transport,
            markji=upstream.markji,
            memo_content=upstream.memo_content,
            study=upstream.study,
            feedback=FeedbackService(database.sessions),
            weakness=WeaknessService(database.sessions),
        )
    finally:
        await _close_stack(stack)


async def _close_stack(stack: AsyncExitStack) -> None:
    with anyio.CancelScope(shield=True):
        cleanup = asyncio.create_task(stack.aclose())
        cancellation: asyncio.CancelledError | None = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as exc:
                if cleanup.cancelled():
                    raise
                cancellation = exc
            except BaseException as exc:
                if cancellation is not None:
                    raise exc from cancellation
                raise
        cleanup.result()
        if cancellation is not None:
            raise cancellation
