"""Transport contracts: no live network, deterministic retry time."""

import hashlib
import hmac
import logging
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

from maimemo_mcp.maimemo_client.errors import (
    AuthenticationError,
    InvalidRequestError,
    RateLimitError,
    UpstreamSchemaError,
    UpstreamUnavailableError,
)
from maimemo_mcp.maimemo_client.transport import MaimemoTransport

TOKEN = "test-private-token"
KEY = "independent-local-key"


class Response(BaseModel):
    model_config = ConfigDict(extra="allow")
    value: int


class Limiter:
    def __init__(self):
        self.fingerprints = []

    async def acquire(self, fingerprint):
        self.fingerprints.append(fingerprint)


async def test_request_uses_pinned_production_server_prefix():
    urls = []

    def handle(request):
        urls.append(str(request.url))
        return httpx.Response(200, json={"value": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = MaimemoTransport(SecretStr(TOKEN), SecretStr(KEY), Limiter(), client=client)
        await transport.request(
            "GET", "/api/v1/markji/decks", params={"limit": 2}, response_type=Response
        )
    assert urls == ["https://open.maimemo.com/open/api/v1/markji/decks?limit=2"]


@pytest.mark.parametrize(
    "status,error",
    [
        (401, AuthenticationError),
        (403, AuthenticationError),
        (400, InvalidRequestError),
        (422, InvalidRequestError),
    ],
)
async def test_terminal_errors_are_safe_and_not_retried(status, error, caplog):
    caplog.set_level(logging.DEBUG)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json={"secret": TOKEN})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = MaimemoTransport(SecretStr(TOKEN), SecretStr(KEY), Limiter(), client=client)
        with pytest.raises(error) as caught:
            await transport.request("GET", "/api/test", response_type=Response)
        assert len(calls) == 1
        assert TOKEN not in repr(caught.value)
        assert TOKEN not in repr(transport)
        assert caught.value.__context__ is None
        assert TOKEN not in caplog.text
        assert "Authorization" not in caplog.text


@pytest.mark.parametrize("retry_after", ["7", "Fri, 02 Oct 2026 00:00:07 GMT"])
async def test_429_honors_retry_after(retry_after):
    calls = []
    waits = []
    limiter = Limiter()

    async def sleep(delay):
        waits.append(delay)

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return httpx.Response(200, json={"value": 1, "future": {"a": 2}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = MaimemoTransport(
            SecretStr(TOKEN),
            SecretStr(KEY),
            limiter,
            client=client,
            sleep=sleep,
            clock=lambda: datetime(2026, 10, 2, tzinfo=UTC),
        )
        result = await transport.request("GET", "/api/test", response_type=Response)
        assert waits == [7.0]
        assert result.model_dump() == {"value": 1, "future": {"a": 2}}
        assert (
            limiter.fingerprints
            == [hmac.new(KEY.encode(), TOKEN.encode(), hashlib.sha256).hexdigest()] * 2
        )
        assert calls[0].headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize(
    "failure,error",
    [("timeout", UpstreamUnavailableError), (503, UpstreamUnavailableError), (429, RateLimitError)],
)
async def test_transient_failures_have_bounded_retries(failure, error):
    calls = []
    waits = []

    async def sleep(delay):
        waits.append(delay)

    def handle(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout(TOKEN, request=request)
        return httpx.Response(failure, text=TOKEN)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = MaimemoTransport(
            SecretStr(TOKEN),
            SecretStr(KEY),
            Limiter(),
            client=client,
            sleep=sleep,
            jitter=lambda: 0.0,
        )
        with pytest.raises(error) as caught:
            await transport.request("GET", "/api/test", response_type=Response)
        assert len(calls) == 3
        assert waits == [1.0, 2.0]
        assert TOKEN not in str(caught.value)
        assert caught.value.__context__ is None


@pytest.mark.parametrize("body", [{"secret": TOKEN}, {"value": TOKEN}, "invalid-json"])
async def test_invalid_schema_does_not_leak_payload(body):
    def handle(request):
        if isinstance(body, str):
            return httpx.Response(200, text=body)
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        transport = MaimemoTransport(SecretStr(TOKEN), SecretStr(KEY), Limiter(), client=client)
        with pytest.raises(UpstreamSchemaError) as caught:
            await transport.request("GET", "/api/test", response_type=Response)
        assert TOKEN not in repr(caught.value)
        assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "path", ["https://evil.example/api", "//evil.example/api", "/api?secret=x"]
)
async def test_transport_rejects_nonrelative_or_embedded_query_paths(path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as client:
        transport = MaimemoTransport(SecretStr(TOKEN), SecretStr(KEY), Limiter(), client=client)
        with pytest.raises(InvalidRequestError):
            await transport.request("GET", path, response_type=Response)


@pytest.mark.parametrize(
    "token",
    [
        "private-secret-é",
        "private-secret\r\nInjected: yes",
        "private-secret\x00",
        "private-secret\x7f",
    ],
)
async def test_invalid_credential_is_rejected_without_sensitive_exception_or_logs(token, caplog):
    caplog.set_level(logging.DEBUG)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"value": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(AuthenticationError) as caught:
            transport = MaimemoTransport(SecretStr(token), SecretStr(KEY), Limiter(), client=client)
            await transport.request("GET", "/api/test", response_type=Response)
    error = caught.value
    assert type(error) is AuthenticationError
    assert str(error) == "Invalid API credential"
    for representation in (
        str(error),
        repr(error),
        repr(error.args),
        repr(error.__cause__),
        repr(error.__context__),
        caplog.text,
    ):
        assert token not in representation
        assert "Authorization" not in representation
        assert "Bearer" not in representation
    assert error.__cause__ is None
    assert error.__context__ is None
    assert calls == []
