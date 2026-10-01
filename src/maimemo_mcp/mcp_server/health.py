"""Public health endpoints contain process/dependency status and safe reason codes only."""

import asyncio
from collections.abc import Callable

from sqlalchemy import text
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from maimemo_mcp.mcp_server.dependencies import Dependencies


def health_routes(dependencies: Callable[[], Dependencies | None]) -> list[Route]:
    async def live(request: Request) -> JSONResponse:
        return JSONResponse({"status": "alive"})

    async def ready(request: Request) -> JSONResponse:
        current = dependencies()
        reason: str | None = None
        if current is None:
            reason = "not_started"
        elif current.http_client.is_closed:
            reason = "http_client_closed"
        else:
            try:
                async with asyncio.timeout(3.0), current.engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
            except Exception:
                # Never send the driver exception, DB URL, credentials or learning rows.
                reason = "database_unavailable"
        if reason is not None:
            return JSONResponse({"status": "not_ready", "reason": reason}, status_code=503)
        return JSONResponse({"status": "ready"})

    return [
        Route("/health/live", live, methods=["GET"]),
        Route("/health/ready", ready, methods=["GET"]),
    ]
