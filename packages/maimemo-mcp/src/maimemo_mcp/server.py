"""Standard MCP v2 Streamable HTTP application; construction performs no secret/database I/O."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from mcp.server import MCPServer as SDKMCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.types import Receive, Scope, Send

from maimemo_mcp.config import MCPSettings
from maimemo_mcp.dependencies import MCPDependencies, open_mcp_dependencies
from maimemo_mcp.health import health_routes
from maimemo_mcp.tools import composite, feedback, markji, memo_content, study
from maimemo_mcp.tools.common import Clock, utc_now

SERVER_INSTRUCTIONS = (
    "The 17 Maimemo tools are read-only upstream operations. "
    "Feedback tools append only local append-only events and never modify upstream data. "
    "Results report their source, UTC fetched_at, completeness and warning codes."
)


@dataclass(repr=False)
class MCPServer:
    """Callable ASGI app with a public SDK server for subsequent tool registration.

    Run this object or ``asgi_app`` with the ASGI lifespan enabled. The SDK owns
    one lifespan per HTTP session manager, including in stateless mode.
    ``Client(server.sdk)`` exercises the same lifespan over in-process transport.
    """

    sdk: SDKMCPServer[MCPDependencies]
    asgi_app: Starlette
    dependencies: MCPDependencies | None = field(default=None, init=False)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.asgi_app(scope, receive, send)


def create_mcp_app(settings: MCPSettings, *, clock: Clock = utc_now) -> MCPServer:
    @asynccontextmanager
    async def lifespan(
        sdk: SDKMCPServer[MCPDependencies],
    ) -> AsyncIterator[MCPDependencies]:
        if server.dependencies is not None:
            raise RuntimeError("MCP application lifespan is already active")
        async with open_mcp_dependencies(settings) as dependencies:
            server.dependencies = dependencies
            try:
                yield dependencies
            finally:
                server.dependencies = None

    sdk = SDKMCPServer[MCPDependencies](
        name="maimemo-mcp",
        version="0.2.0",
        instructions=SERVER_INSTRUCTIONS,
        log_level=settings.core.log_level,
        lifespan=lifespan,
    )
    markji.register(sdk, clock)
    memo_content.register(sdk, clock)
    study.register(sdk, clock)
    composite.register(sdk, clock)
    feedback.register(sdk, clock)
    hosts = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*"]
    if settings.host not in ("0.0.0.0", "::", "127.0.0.1", "localhost", "::1"):
        hosts.extend([settings.host, f"{settings.host}:*"])
    for host in settings.allowed_hosts:
        hosts.extend([host, f"{host}:*"])
    asgi_app = sdk.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=[]),
    )
    # Exact paths only: don't issue redirects for tool or public health requests.
    asgi_app.router.redirect_slashes = False
    server = MCPServer(sdk=sdk, asgi_app=asgi_app)
    asgi_app.routes.extend(
        health_routes(
            lambda: server.dependencies,
            drift_state_file=settings.drift_state_file,
        )
    )
    return server
