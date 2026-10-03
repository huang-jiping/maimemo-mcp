"""Minimal public HTTP application for health and future OAuth routes."""

from __future__ import annotations

from maimemo.storage.database import create_async_engine
from sqlalchemy import text
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from maimemo_server.config import ServerSettings


async def _home(request: Request) -> JSONResponse:
    return JSONResponse({"service": "maimemo-server", "status": "ok"})


async def _live(request: Request) -> JSONResponse:
    return JSONResponse({"status": "live"})


async def _oauth_not_configured(request: Request) -> JSONResponse:
    return JSONResponse({"error": "oauth_not_configured"}, status_code=503)


def create_app(settings: ServerSettings) -> Starlette:
    async def ready(request: Request) -> JSONResponse:
        engine = create_async_engine(settings.database.database_url)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            if revision != "0004":
                return JSONResponse({"status": "not_ready"}, status_code=503)
        except Exception:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        finally:
            await engine.dispose()
        return JSONResponse({"status": "ready"})

    return Starlette(
        routes=[
            Route("/", _home, methods=["GET"]),
            Route("/health/live", _live, methods=["GET"]),
            Route("/health/ready", ready, methods=["GET"]),
            Route("/oauth/start", _oauth_not_configured, methods=["GET"]),
            Route("/oauth/callback", _oauth_not_configured, methods=["GET"]),
        ]
    )
