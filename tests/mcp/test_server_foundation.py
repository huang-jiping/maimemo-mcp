"""Real SDK/ASGI contracts; only database I/O and upstream network are isolated."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from mcp.client import Client, ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from maimemo_mcp.config import Settings
from maimemo_mcp.mcp_server.app import create_mcp_app
from maimemo_mcp.mcp_server.envelopes import Completeness, ToolEnvelope, ToolMeta


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    token = tmp_path / "token"
    key = tmp_path / "fingerprint-key"
    token.write_text("test-token-only", encoding="utf-8")
    key.write_text("test-key-only", encoding="utf-8")
    return Settings(
        database_url="postgresql+psycopg://test_only:test_only@127.0.0.1:1/mcp_test",
        token_file=token,
        token_fingerprint_key_file=key,
    )


class DatabaseBoundary:
    """No socket is opened: retain the real engine, replace just DB I/O."""

    def __init__(self) -> None:
        self.failure = False
        self.statements: list[str] = []

    @asynccontextmanager
    async def connect(self, engine: AsyncEngine) -> AsyncIterator["DatabaseBoundary"]:
        yield self

    async def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))
        if self.failure:
            raise RuntimeError("test-token-only postgresql://secret personal-learning-statistics")


@pytest.fixture
def database_boundary(monkeypatch: pytest.MonkeyPatch) -> DatabaseBoundary:
    boundary = DatabaseBoundary()

    def connect(engine: AsyncEngine) -> Any:
        return boundary.connect(engine)

    monkeypatch.setattr(AsyncEngine, "connect", connect)
    return boundary


async def test_server_discovery_uses_mcp_v2_in_process(settings: Settings) -> None:
    # A missing SDK server or protocol discovery/instructions breaks this boundary.
    server = create_mcp_app(settings)
    assert server.dependencies is None
    async with Client(server.sdk) as client:
        assert client.protocol_version is not None
        assert (await client.list_tools()).tools == []
        assert client.instructions is not None
        assert "17" in client.instructions
        assert "read-only" in client.instructions
        assert "append-only" in client.instructions
        assert server.dependencies is not None
        http_client = server.dependencies.http_client
    assert server.dependencies is None
    assert http_client.is_closed


async def test_standard_streamable_http_client_and_routes(
    settings: Settings, database_boundary: DatabaseBoundary
) -> None:
    # A wrong route, wrong HTTP media type or stateful session leaks fails real HTTP discovery.
    server = create_mcp_app(settings)
    async with server.asgi_app.router.lifespan_context(server.asgi_app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=server), base_url="http://localhost"
        ) as http_client:
            async with streamable_http_client(
                "http://localhost/mcp", http_client=http_client
            ) as streams:
                async with ClientSession(*streams) as session:
                    result = await session.discover()
                    assert result.instructions is not None
                    assert (await session.list_tools()).tools == []
            response = await http_client.post(
                "/mcp",
                headers={"Accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "test-client", "version": "1"},
                    },
                },
            )
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("application/json")
            assert "mcp-session-id" not in response.headers
            assert response.json()["result"]["serverInfo"]["name"] == "maimemo-mcp"
            for path in ("/", "/mcp/tools", "/health", "/health/ready/"):
                missing = await http_client.get(path)
                assert missing.status_code == 404
            preflight = await http_client.options(
                "/mcp", headers={"Origin": "https://untrusted.example"}
            )
            assert "access-control-allow-origin" not in preflight.headers
            forbidden = await http_client.post(
                "/mcp",
                headers={
                    "Origin": "https://untrusted.example",
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
            )
            assert forbidden.status_code == 403


async def test_health_routes_expose_only_safe_dependency_status(
    settings: Settings, database_boundary: DatabaseBoundary
) -> None:
    # Accidentally querying learning tables or returning raw errors/secrets violates health scope.
    server = create_mcp_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server), base_url="http://localhost"
    ) as client:
        live = await client.get("/health/live")
        assert live.status_code == 200
        assert live.json() == {"status": "alive"}
        not_started = await client.get("/health/ready")
        assert not_started.status_code == 503
        assert not_started.json() == {"status": "not_ready", "reason": "not_started"}
        async with server.asgi_app.router.lifespan_context(server.asgi_app):
            ready = await client.get("/health/ready")
            assert ready.status_code == 200
            assert ready.headers["content-type"] == "application/json"
            assert ready.json() == {"status": "ready"}
            assert database_boundary.statements == ["SELECT 1"]
            database_boundary.failure = True
            failed = await client.get("/health/ready")
            assert failed.status_code == 503
            assert failed.json() == {"status": "not_ready", "reason": "database_unavailable"}
            assert (await client.get("/health/live")).json() == {"status": "alive"}
            assert server.dependencies is not None
            await server.dependencies.http_client.aclose()
            closed = await client.get("/health/ready")
            assert closed.status_code == 503
            assert closed.json() == {"status": "not_ready", "reason": "http_client_closed"}


def test_tool_envelope_serializes_utc_and_completeness() -> None:
    # Preserving a local offset or accepting an unknown quality hides temporal/data quality errors.
    envelope = ToolEnvelope[dict[str, int]](
        data={"count": 3},
        meta=ToolMeta(
            source=["local_history"],
            fetched_at=datetime(2026, 10, 2, 8, tzinfo=timezone(timedelta(hours=8))),
            data_through=date(2026, 10, 1),
            completeness=Completeness.PARTIAL,
            warnings=["missing_history"],
        ),
    )
    assert envelope.meta.fetched_at == datetime(2026, 10, 2, tzinfo=UTC)
    assert envelope.model_dump(mode="json") == {
        "data": {"count": 3},
        "meta": {
            "source": ["local_history"],
            "fetched_at": "2026-10-02T00:00:00Z",
            "data_through": "2026-10-01",
            "completeness": "partial",
            "warnings": ["missing_history"],
        },
    }
    for value in ("complete", "partial", "stale", "unavailable"):
        assert ToolMeta.model_validate(
            {"source": [], "fetched_at": datetime.now(UTC), "completeness": value}
        ).model_dump(mode="json")["completeness"] == value
    for update in (
        {"fetched_at": datetime(2026, 10, 2)},
        {"completeness": "made_up"},
        {"source": [SecretStr("test-token-only")]},
        {"warnings": ["Bearer test-token-only"]},
        {"source": ["postgresql://user:secret@db/name"]},
    ):
        payload = envelope.meta.model_dump()
        payload.update(update)
        with pytest.raises(ValidationError):
            ToolMeta.model_validate(payload)


@pytest.mark.parametrize("failure", [None, "startup", "body"])
async def test_lifespan_closes_owned_resources_once(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    # Missing partial-startup unwind or duplicate shutdown leaks/double-closes actual resources.
    dispose_calls = 0
    close_calls = 0
    real_dispose = AsyncEngine.dispose
    real_close = httpx.AsyncClient.aclose

    async def dispose(engine: AsyncEngine, close: bool = True) -> None:
        nonlocal dispose_calls
        dispose_calls += 1
        await real_dispose(engine, close=close)

    async def close(client: httpx.AsyncClient) -> None:
        nonlocal close_calls
        close_calls += 1
        await real_close(client)

    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    monkeypatch.setattr(httpx.AsyncClient, "aclose", close)
    if failure == "startup":
        settings.token_fingerprint_key_file.unlink()
    server = create_mcp_app(settings)
    assert (dispose_calls, close_calls) == (0, 0)
    resources = None
    try:
        async with server.asgi_app.router.lifespan_context(server.asgi_app):
            resources = server.dependencies
            assert resources is not None
            assert resources.http_client.follow_redirects is False
            assert resources.feedback.session_factory is resources.sessions
            assert resources.weakness.session_factory is resources.sessions
            if failure == "body":
                raise RuntimeError("body failed")
    except (FileNotFoundError, ExceptionGroup) as exc:
        assert failure in ("startup", "body"), str(exc)
    assert server.dependencies is None
    assert (dispose_calls, close_calls) == (1, 1)
    if resources is not None:
        assert resources.http_client.is_closed


def test_app_construction_does_not_read_secret_files_or_open_connections(
    settings: Settings,
) -> None:
    # Construction must remain inert so importing/configuring the app has no I/O.
    settings.token_file.unlink()
    settings.token_fingerprint_key_file.unlink()
    server = create_mcp_app(settings)
    assert server.dependencies is None
