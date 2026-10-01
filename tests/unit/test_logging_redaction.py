"""Logs and operational health expose only explicit, non-personal fields."""

import io
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from starlette.applications import Starlette

from maimemo_mcp.config import Settings
from maimemo_mcp.logging import SafeJsonFormatter, configure_logging, log_event
from maimemo_mcp.mcp_server.health import health_routes

SECRET = "token-value-ABC123"


def settings(tmp_path: Path) -> Settings:
    token = tmp_path / "token"
    key = tmp_path / "key"
    token.write_text(SECRET, encoding="utf-8")
    key.write_text("fingerprint-key-secret", encoding="utf-8")
    return Settings(
        database_url="postgresql+psycopg://user:password@localhost/db",
        token_file=token,
        token_fingerprint_key_file=key,
    )


def test_structured_log_retains_allowlisted_metrics_and_drops_sensitive_values(
    tmp_path: Path,
) -> None:
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(SafeJsonFormatter(secrets=(SECRET, "fingerprint-key-secret")))
    logger = logging.getLogger("maimemo-test-redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    try:
        raise RuntimeError(
            f"Authorization: Bearer {SECRET}; personal payload={{'word': 'private-word'}}"
        )
    except RuntimeError as exc:
        log_event(
            logger,
            "upstream_request",
            endpoint="/api/v1/study/records",
            latency_ms=12.5,
            status=503,
            trace_id="trace-123",
            error=exc,
            authorization=f"Bearer {SECRET}",
            response_payload={"word": "private-word", "token": SECRET},
        )

    raw = output.getvalue()
    parsed = json.loads(raw)
    assert parsed["event"] == "upstream_request"
    assert parsed["endpoint"] == "/api/v1/study/records"
    assert parsed["latency_ms"] == 12.5
    assert parsed["status"] == 503
    assert parsed["trace_id"] == "trace-123"
    assert parsed["error_class"] == "RuntimeError"
    for forbidden in (
        SECRET,
        "fingerprint-key-secret",
        "Authorization",
        "Bearer",
        "private-word",
        "response_payload",
        "password",
    ):
        assert forbidden not in raw


def test_configure_logging_does_not_render_secret_paths_or_values(tmp_path: Path) -> None:
    app_settings = settings(tmp_path)
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    try:
        configure_logging(app_settings)
        assert root.level == logging.INFO
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, SafeJsonFormatter)
        record = logging.LogRecord(
            "unsafe",
            logging.ERROR,
            __file__,
            1,
            f"{SECRET} postgresql://user:password@localhost/db",
            (),
            None,
        )
        rendered = root.handlers[0].formatter.format(record)
        assert SECRET not in rendered
        assert "password" not in rendered
        assert "postgresql" not in rendered
    finally:
        root.handlers = original_handlers
        root.setLevel(original_level)


class FakeScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class FakeSession:
    async def scalars(self, statement: Any) -> FakeScalarResult:
        assert "ingestion_run" in str(statement)
        return FakeScalarResult(
            [
                SimpleNamespace(
                    task_type="today",
                    status="failed",
                    finished_at=datetime(2026, 10, 2, 3, tzinfo=UTC),
                ),
                SimpleNamespace(
                    task_type="today",
                    status="complete",
                    finished_at=datetime(2026, 10, 2, 2, tzinfo=UTC),
                ),
                SimpleNamespace(
                    task_type="records",
                    status="partial",
                    finished_at=datetime(2026, 10, 2, 1, tzinfo=UTC),
                ),
            ]
        )

    async def scalar(self, statement: Any) -> str:
        assert "schema_metadata" in str(statement)
        return "0003"


class FakeSessions:
    @asynccontextmanager
    async def __call__(self) -> Any:
        yield FakeSession()


async def test_operational_health_has_safe_required_fields(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "maimemo_mcp.mcp_server.health.pinned_schema_hash",
        lambda: "a" * 64,
    )
    dependencies = SimpleNamespace(sessions=FakeSessions())
    app = Starlette(routes=health_routes(lambda: dependencies))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.get("/health/status")

    assert response.status_code == 200
    assert response.json() == {
        "status": "available",
        "last_successful_collection": "2026-10-02T02:00:00Z",
        "consecutive_failures": {"records": 0, "today": 1},
        "schema_hash": "a" * 64,
        "drift_status": "unavailable",
        "database_migration_revision": "0003",
    }
    rendered = response.text
    for forbidden in ("private", "payload", "Authorization", "Bearer", "password"):
        assert forbidden not in rendered
