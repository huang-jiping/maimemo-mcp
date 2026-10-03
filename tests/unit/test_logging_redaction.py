"""Logs and operational health expose only explicit, non-personal fields."""

import asyncio
import io
import json
import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from maimemo.api_client.errors import UpstreamSchemaError, UpstreamUnavailableError
from maimemo.api_client.transport import MaimemoTransport
from maimemo.ingestion.service import IngestionResult
from maimemo.logging import SafeJsonFormatter, configure_logging, log_event
from maimemo_mcp.config import MCPSettings as Settings
from maimemo_mcp.health import health_routes, operational_health
from maimemo_worker.worker import Worker
from pydantic import BaseModel, SecretStr
from starlette.applications import Starlette

SECRET = "token-value-ABC123"


class TransportResponse(BaseModel):
    value: int


class YieldingLimiter:
    async def acquire(self, fingerprint: str) -> None:
        await asyncio.sleep(0)


async def no_sleep(delay: float) -> None:
    await asyncio.sleep(0)


@contextmanager
def captured_safe_logs(logger_name: str) -> Iterator[io.StringIO]:
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(SafeJsonFormatter(secrets=(SECRET,)))
    logger = logging.getLogger(logger_name)
    old_handlers = logger.handlers[:]
    old_level = logger.level
    old_propagate = logger.propagate
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        yield output
    finally:
        logger.handlers = old_handlers
        logger.setLevel(old_level)
        logger.propagate = old_propagate


def settings(tmp_path: Path) -> Settings:
    token = tmp_path / "token"
    key = tmp_path / "key"
    token.write_text(SECRET, encoding="utf-8")
    key.write_text("fingerprint-key-secret", encoding="utf-8")
    return Settings.load(
        {
            "MAIMEMO_DATABASE_URL": "postgresql+psycopg://user:password@localhost/db",
            "MAIMEMO_TOKEN_FILE": str(token),
            "MAIMEMO_TOKEN_FINGERPRINT_KEY_FILE": str(key),
        }
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
        configure_logging(app_settings.core.log_level)
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
        "maimemo_mcp.health.pinned_schema_hash",
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


@pytest.mark.parametrize("severity", ["none", "informational", "high"])
async def test_operational_health_reads_fresh_persisted_drift_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    severity: str,
) -> None:
    checked_at = datetime(2026, 10, 2, 2, tzinfo=UTC)
    state = tmp_path / "drift.json"
    state.write_text(
        json.dumps(
            {
                "severity": severity,
                "checked_at": "2026-10-02T02:00:00Z",
                "pinned_sha256": "a" * 64,
                "current_sha256": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "maimemo_mcp.health.pinned_schema_hash", lambda: "a" * 64
    )
    result = await operational_health(
        SimpleNamespace(sessions=FakeSessions()),
        drift_state_file=state,
        now=checked_at + timedelta(hours=1),
    )
    assert result["drift_status"] == severity
    assert result["drift_checked_at"] == "2026-10-02T02:00:00Z"
    assert result["drift_pinned_hash"] == "a" * 64
    assert result["drift_remote_hash"] == "b" * 64


async def test_operational_health_marks_old_or_mismatched_drift_state_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "drift.json"
    state.write_text(
        json.dumps(
            {
                "severity": "none",
                "checked_at": "2026-10-01T00:00:00Z",
                "pinned_sha256": "b" * 64,
                "current_sha256": "c" * 64,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "maimemo_mcp.health.pinned_schema_hash", lambda: "a" * 64
    )
    result = await operational_health(
        SimpleNamespace(sessions=FakeSessions()),
        drift_state_file=state,
        now=datetime(2026, 10, 2, 3, tzinfo=UTC),
    )
    assert result["drift_status"] == "stale"
    assert result["last_drift_severity"] == "none"


async def test_malformed_drift_state_is_bounded_and_cannot_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "drift.json"
    state.write_text(
        json.dumps({"severity": f"Bearer {SECRET}", "payload": "private-word"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "maimemo_mcp.health.pinned_schema_hash", lambda: "a" * 64
    )
    result = await operational_health(
        SimpleNamespace(sessions=FakeSessions()),
        drift_state_file=state,
        now=datetime(2026, 10, 2, tzinfo=UTC),
    )
    assert result["drift_status"] == "unavailable"
    assert SECRET not in json.dumps(result) and "private-word" not in json.dumps(result)


async def test_transport_logs_each_retry_and_limiter_wait_with_fixed_endpoint() -> None:
    calls = 0
    ticks = iter(float(value) for value in range(20))

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text=SECRET)
        return httpx.Response(200, json={"value": 1, "private": SECRET})

    with captured_safe_logs("maimemo.api_client.transport") as output:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            transport = MaimemoTransport(
                SecretStr(SECRET),
                SecretStr("safe-fingerprint-key"),
                YieldingLimiter(),
                client=client,
                sleep=no_sleep,
                monotonic=lambda: next(ticks),
                trace_id_factory=lambda: "trace-retry",
            )
            result = await transport.request(
                "GET",
                "/api/v1/markji/decks/private-deck/cards/private-card",
                response_type=TransportResponse,
            )
    assert result.value == 1
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    requests = [record for record in records if record["event"] == "upstream_request"]
    waits = [record for record in records if record["event"] == "rate_limit_wait"]
    assert [record["status"] for record in requests] == [429, 200]
    assert requests[0]["error_class"] == "RateLimitError"
    assert all(
        record["endpoint"] == "/api/v1/markji/decks/{deck}/cards/{card}"
        for record in records
    )
    assert all(record["trace_id"] == "trace-retry" for record in records)
    assert len(waits) == 2 and all(record["latency_ms"] >= 0 for record in waits)
    rendered = output.getvalue()
    for forbidden in (SECRET, "private-deck", "private-card", "Authorization", "Bearer"):
        assert forbidden not in rendered


@pytest.mark.parametrize(
    ("mode", "expected_exception", "expected_error_class"),
    [
        ("network", UpstreamUnavailableError, "UpstreamUnavailableError"),
        ("schema", UpstreamSchemaError, "UpstreamSchemaError"),
    ],
)
async def test_transport_logs_safe_controlled_error_classes(
    mode: str,
    expected_exception: type[Exception],
    expected_error_class: str,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if mode == "network":
            raise httpx.ReadTimeout(f"Bearer {SECRET}", request=request)
        return httpx.Response(200, json={"value": SECRET, "personal": "private-word"})

    with captured_safe_logs("maimemo.api_client.transport") as output:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            transport = MaimemoTransport(
                SecretStr(SECRET),
                SecretStr("safe-fingerprint-key"),
                YieldingLimiter(),
                client=client,
                max_attempts=1,
                trace_id_factory=lambda: "trace-error",
            )
            with pytest.raises(expected_exception):
                await transport.request(
                    "GET", "/api/v1/memo/notes", response_type=TransportResponse
                )
    request_record = next(
        json.loads(line)
        for line in output.getvalue().splitlines()
        if json.loads(line)["event"] == "upstream_request"
    )
    assert request_record["error_class"] == expected_error_class
    assert request_record["trace_id"] == "trace-error"
    assert SECRET not in output.getvalue() and "private-word" not in output.getvalue()


async def test_concurrent_transport_contexts_keep_trace_ids_isolated() -> None:
    trace_ids = iter(("trace-a", "trace-b"))

    async def handle(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        return httpx.Response(200, json={"value": 1})

    with captured_safe_logs("maimemo.api_client.transport") as output:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            transport = MaimemoTransport(
                SecretStr(SECRET),
                SecretStr("safe-fingerprint-key"),
                YieldingLimiter(),
                client=client,
                trace_id_factory=lambda: next(trace_ids),
            )
            await asyncio.gather(
                transport.request(
                    "POST",
                    "/api/v1/memo/study/get_study_progress",
                    response_type=TransportResponse,
                ),
                transport.request(
                    "POST",
                    "/api/v1/memo/study/get_today_items",
                    response_type=TransportResponse,
                ),
            )
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    by_endpoint: dict[str, set[str]] = {}
    for record in records:
        by_endpoint.setdefault(record["endpoint"], set()).add(record["trace_id"])
    assert len(by_endpoint) == 2
    assert {next(iter(values)) for values in by_endpoint.values()} == {"trace-a", "trace-b"}
    assert all(len(values) == 1 for values in by_endpoint.values())


async def test_worker_logs_absorbed_collection_failure_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = object.__new__(Worker)

    async def failed(job: Any, now: datetime) -> IngestionResult:
        return IngestionResult(None, "failed", 1, 0, ["safe failure"], "collection")

    monkeypatch.setattr(worker, "_run_job", failed)
    job = SimpleNamespace(identity="today")
    with captured_safe_logs("maimemo_worker.worker") as output:
        result = await worker.run_job(job, datetime.now(UTC))
    assert result.status == "failed"
    record = json.loads(output.getvalue())
    assert record["status"] == "failed"
    assert record["error_class"] == "collection"
