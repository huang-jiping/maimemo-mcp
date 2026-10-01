"""Public health endpoints contain process/dependency status and safe reason codes only."""

import asyncio
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from maimemo_mcp.mcp_server.dependencies import Dependencies
from maimemo_mcp.storage.base import SchemaMetadata
from maimemo_mcp.storage.models.ingestion import IngestionRun

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def pinned_schema_hash() -> str:
    checksum = (
        Path(__file__).resolve().parents[3] / "openapi" / "maimemo-api.sha256"
    ).read_text(encoding="ascii").strip().split()[0]
    if not _SHA256.fullmatch(checksum):
        raise ValueError("Pinned OpenAPI checksum is invalid")
    return checksum


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


async def operational_health(current: Dependencies) -> dict[str, Any]:
    async with current.sessions() as session:
        runs = list(
            (
                await session.scalars(
                    select(IngestionRun)
                    .where(
                        IngestionRun.task_type.in_(["today", "records"]),
                        IngestionRun.finished_at.is_not(None),
                    )
                    .order_by(IngestionRun.finished_at.desc())
                )
            ).all()
        )
        revision = await session.scalar(
            select(SchemaMetadata.schema_version).order_by(SchemaMetadata.installed_at.desc()).limit(1)
        )
    successful = [run for run in runs if run.status in ("complete", "partial")]
    failures: dict[str, int] = {}
    for task in ("records", "today"):
        count = 0
        for run in (item for item in runs if item.task_type == task):
            if run.status != "failed":
                break
            count += 1
        failures[task] = count
    return {
        "status": "available",
        "last_successful_collection": _iso(
            max(
                (run.finished_at for run in successful if run.finished_at is not None),
                default=None,
            )
        ),
        "consecutive_failures": failures,
        "schema_hash": pinned_schema_hash(),
        # The live drift check is a separate credential-free operational command. Until an
        # operator runs/persists it, health must say unavailable rather than claiming "none".
        "drift_status": "unavailable",
        "database_migration_revision": revision,
    }


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

    async def status(request: Request) -> JSONResponse:
        current = dependencies()
        if current is None:
            return JSONResponse(
                {"status": "unavailable", "reason": "not_started"}, status_code=503
            )
        try:
            async with asyncio.timeout(3.0):
                payload = await operational_health(current)
        except Exception:
            return JSONResponse(
                {"status": "unavailable", "reason": "database_unavailable"}, status_code=503
            )
        return JSONResponse(payload)

    return [
        Route("/health/live", live, methods=["GET"]),
        Route("/health/ready", ready, methods=["GET"]),
        Route("/health/status", status, methods=["GET"]),
    ]
