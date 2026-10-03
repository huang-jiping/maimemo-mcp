"""Public health endpoints contain process/dependency status and safe reason codes only."""

import asyncio
import json
import re
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from maimemo.storage.base import SchemaMetadata
from maimemo.storage.models.ingestion import IngestionRun
from sqlalchemy import select, text
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from maimemo_mcp.mcp_server.dependencies import Dependencies

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DRIFT_STATE_FIELDS = {"severity", "checked_at", "pinned_sha256", "current_sha256"}
_DRIFT_MAX_AGE = timedelta(hours=26)
_DRIFT_MAX_BYTES = 4096


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


def _drift_health(
    path: Path | None,
    *,
    current_schema_hash: str,
    now: datetime,
) -> dict[str, Any]:
    unavailable = {"drift_status": "unavailable"}
    if path is None:
        return unavailable
    try:
        file_status = path.stat()
        if not stat.S_ISREG(file_status.st_mode) or file_status.st_size > _DRIFT_MAX_BYTES:
            return unavailable
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or set(parsed) != _DRIFT_STATE_FIELDS:
            return unavailable
        severity = parsed["severity"]
        checked_text = parsed["checked_at"]
        pinned_hash = parsed["pinned_sha256"]
        remote_hash = parsed["current_sha256"]
        if severity not in ("none", "informational", "high"):
            return unavailable
        if not all(
            isinstance(value, str) and _SHA256.fullmatch(value)
            for value in (pinned_hash, remote_hash)
        ):
            return unavailable
        if not isinstance(checked_text, str) or not checked_text.endswith("Z"):
            return unavailable
        checked_at = datetime.fromisoformat(checked_text.replace("Z", "+00:00"))
        if checked_at.tzinfo is None or checked_at.utcoffset() is None:
            return unavailable
        checked_at = checked_at.astimezone(UTC)
        at = now.astimezone(UTC)
        if checked_at > at + timedelta(minutes=5):
            return unavailable
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return unavailable
    details: dict[str, Any] = {
        "drift_checked_at": _iso(checked_at),
        "drift_pinned_hash": pinned_hash,
        "drift_remote_hash": remote_hash,
    }
    if at - checked_at > _DRIFT_MAX_AGE or pinned_hash != current_schema_hash:
        return {"drift_status": "stale", "last_drift_severity": severity, **details}
    return {"drift_status": severity, **details}


async def operational_health(
    current: Dependencies,
    *,
    drift_state_file: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
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
    schema_hash = pinned_schema_hash()
    result = {
        "status": "available",
        "last_successful_collection": _iso(
            max(
                (run.finished_at for run in successful if run.finished_at is not None),
                default=None,
            )
        ),
        "consecutive_failures": failures,
        "schema_hash": schema_hash,
        "database_migration_revision": revision,
    }
    result.update(
        _drift_health(
            drift_state_file,
            current_schema_hash=schema_hash,
            now=datetime.now(UTC) if now is None else now,
        )
    )
    return result


def health_routes(
    dependencies: Callable[[], Dependencies | None],
    *,
    drift_state_file: Path | None = None,
) -> list[Route]:
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
                payload = await operational_health(
                    current,
                    drift_state_file=drift_state_file,
                )
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
