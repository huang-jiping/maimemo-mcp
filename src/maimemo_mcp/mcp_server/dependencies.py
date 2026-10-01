"""One lifespan owns engines and transports; API/services share those dependencies."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

import anyio
import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from maimemo_mcp.analysis.service import WeaknessService
from maimemo_mcp.config import Settings
from maimemo_mcp.feedback.service import FeedbackService
from maimemo_mcp.maimemo_client.markji import MarkjiClient
from maimemo_mcp.maimemo_client.memo_content import MemoContentClient
from maimemo_mcp.maimemo_client.rate_limit import SharedRateLimiter
from maimemo_mcp.maimemo_client.study import StudyClient
from maimemo_mcp.maimemo_client.transport import MaimemoTransport
from maimemo_mcp.storage.database import create_async_engine_from_settings, create_session_factory


@dataclass(frozen=True, repr=False)
class Dependencies:
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
async def open_dependencies(settings: Settings) -> AsyncIterator[Dependencies]:
    stack = AsyncExitStack()
    try:
        engine = create_async_engine_from_settings(settings)
        stack.push_async_callback(engine.dispose)
        sessions = create_session_factory(engine)
        http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
        stack.push_async_callback(http_client.aclose)
        limiter = SharedRateLimiter(sessions)
        transport = MaimemoTransport(
            settings.read_maimemo_token(),
            settings.read_token_fingerprint_key(),
            limiter,
            client=http_client,
        )
        # The HTTP client is externally owned by this stack; transport won't close it twice.
        stack.push_async_callback(transport.aclose)
        yield Dependencies(
            engine=engine,
            sessions=sessions,
            http_client=http_client,
            limiter=limiter,
            transport=transport,
            markji=MarkjiClient(transport),
            memo_content=MemoContentClient(transport),
            study=StudyClient(transport),
            feedback=FeedbackService(sessions),
            weakness=WeaknessService(sessions),
        )
    finally:
        await _close_stack(stack)


async def _close_stack(stack: AsyncExitStack) -> None:
    # AnyIO shields its own scope cancellation, while asyncio shields native Task.cancel().
    # Keep the cleanup task alive and awaited even if shutdown receives repeated cancellations.
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
                # Cleanup failure takes precedence, retaining any interrupted shutdown as cause.
                if cancellation is not None:
                    raise exc from cancellation
                raise
        cleanup.result()
        if cancellation is not None:
            raise cancellation
