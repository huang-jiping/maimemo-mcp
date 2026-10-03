"""Worker-owned OpenAPI monitoring with a recoverable six-hour polling loop."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import httpx
from maimemo import openapi_drift
from maimemo.logging import log_event

logger = logging.getLogger(__name__)
CHECK_INTERVAL_SECONDS = 6 * 60 * 60


class OpenApiDriftMonitor:
    def __init__(
        self,
        state_file: Path,
        *,
        pinned_file: Path = openapi_drift.DEFAULT_PINNED_FILE,
        remote_url: str = openapi_drift.DEFAULT_REMOTE_URL,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.state_file = state_file
        self.pinned_file = pinned_file
        self.remote_url = remote_url
        self.client = client
        self.sleep = sleep
        self.clock = clock

    async def run_forever(self) -> None:
        if self.client is not None:
            await self._run(self.client)
        else:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                await self._run(client)

    async def _run(self, client: httpx.AsyncClient) -> None:
        while True:
            try:
                pinned = openapi_drift.read_pinned(self.pinned_file)
                current = await openapi_drift.fetch_openapi(client, self.remote_url)
                report = await self._compare(pinned, current)
                openapi_drift._write_state(self.state_file, report, self.clock())
            except Exception as error:
                # Failed checks leave the last successful state untouched; its age
                # continues to increase so readers can eventually report it stale.
                log_event(
                    logger,
                    "openapi_drift_check",
                    status="failed",
                    error_class=openapi_drift.safe_error_category(error),
                )
            else:
                log_event(logger, "openapi_drift_check", status=report.severity)
            await self.sleep(CHECK_INTERVAL_SECONDS)

    @staticmethod
    async def _compare(pinned: bytes, current: bytes) -> openapi_drift.DriftReport:
        stop = Event()
        task = asyncio.create_task(
            asyncio.to_thread(openapi_drift.compare_openapi, pinned, current, stop=stop)
        )
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
                stop.set()
            except Exception:
                break
        if cancelled:
            if not task.cancelled():
                task.exception()
            raise asyncio.CancelledError
        return task.result()
