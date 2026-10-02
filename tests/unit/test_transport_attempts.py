"""Actual-send accounting stays scoped to one async task and retains no payload."""

import asyncio

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from maimemo_mcp.maimemo_client.errors import UpstreamUnavailableError
from maimemo_mcp.maimemo_client.transport import MaimemoTransport, observe_http_attempts


class Response(BaseModel):
    value: int


class NoWaitLimiter:
    async def acquire(self, fingerprint: str) -> None:
        return None


async def no_sleep(delay: float) -> None:
    await asyncio.sleep(0)


async def test_concurrent_transport_scopes_count_their_own_retries() -> None:
    sent = {"retry": 0, "single": 0}

    async def handle(request: httpx.Request) -> httpx.Response:
        key = request.url.path.rsplit("/", 1)[-1]
        sent[key] += 1
        await asyncio.sleep(0)
        return httpx.Response(500 if key == "retry" and sent[key] < 3 else 200, json={"value": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = MaimemoTransport(
            SecretStr("synthetic-token"),
            SecretStr("synthetic-key"),
            NoWaitLimiter(),
            client=client,
            sleep=no_sleep,
        )

        async def collect(key: str) -> int:
            with observe_http_attempts() as observation:
                await transport.request("GET", f"/api/{key}", response_type=Response)
            return observation.count

        counts = await asyncio.gather(collect("retry"), collect("single"))
        assert list(counts) == [3, 1]
    assert sent == {"retry": 3, "single": 1}


async def test_nested_scopes_restore_outer_scope_and_exit_stops_counting() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"value": 1}))
    ) as client:
        transport = MaimemoTransport(
            SecretStr("synthetic-token"), SecretStr("synthetic-key"), NoWaitLimiter(), client=client
        )
        with observe_http_attempts() as outer:
            await transport.request("GET", "/api/value", response_type=Response)
            with observe_http_attempts() as inner:
                await transport.request("GET", "/api/value", response_type=Response)
            await transport.request("GET", "/api/value", response_type=Response)
        await transport.request("GET", "/api/value", response_type=Response)
        assert outer.count == 2 and inner.count == 1
        assert "synthetic-token" not in repr(outer)


async def test_inherited_context_in_child_task_does_not_mutate_parent_counter() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"value": 1}))
    ) as client:
        transport = MaimemoTransport(
            SecretStr("synthetic-token"), SecretStr("synthetic-key"), NoWaitLimiter(), client=client
        )
        with observe_http_attempts() as parent:
            await asyncio.create_task(
                transport.request("GET", "/api/value", response_type=Response)
            )
            assert parent.count == 0
            await transport.request("GET", "/api/value", response_type=Response)
        assert parent.count == 1


async def test_exception_exit_cleans_scope_without_body_or_token() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, text="Bearer synthetic-token private-body")
        )
    ) as client:
        transport = MaimemoTransport(
            SecretStr("synthetic-token"),
            SecretStr("synthetic-key"),
            NoWaitLimiter(),
            client=client,
            max_attempts=1,
        )
        with pytest.raises(UpstreamUnavailableError) as error:
            with observe_http_attempts() as observation:
                await transport.request("GET", "/api/value", response_type=Response)
        with pytest.raises(UpstreamUnavailableError):
            await transport.request("GET", "/api/value", response_type=Response)
        assert observation.count == 1
        assert "synthetic-token" not in repr(observation) + str(error.value)
        assert "private-body" not in repr(observation) + str(error.value)


async def test_limiter_failure_before_http_send_is_not_counted() -> None:
    class FailingLimiter:
        async def acquire(self, fingerprint: str) -> None:
            raise ValueError("synthetic limiter unavailable")

    sent = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent += 1
        return httpx.Response(200, json={"value": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = MaimemoTransport(
            SecretStr("synthetic-token"),
            SecretStr("synthetic-key"),
            FailingLimiter(),
            client=client,
        )
        with observe_http_attempts() as observation:
            with pytest.raises(ValueError):
                await transport.request("GET", "/api/value", response_type=Response)
        assert observation.count == 0 and sent == 0
