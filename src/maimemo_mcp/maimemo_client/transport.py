"""Bounded retry policy with explicit secret and upstream error boundaries."""

import asyncio
import hashlib
import hmac
import random
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from maimemo_mcp.maimemo_client.errors import (
    AuthenticationError,
    InvalidRequestError,
    RateLimitError,
    UpstreamSchemaError,
    UpstreamUnavailableError,
)
from maimemo_mcp.maimemo_client.rate_limit import utc_now

T = TypeVar("T", bound=BaseModel)
BASE_URL = "https://open.maimemo.com/open"


@dataclass
class HttpAttemptObservation:
    """Only a counter; ownership prevents copied child contexts mutating their parent."""

    count: int = 0
    _owner_task_id: int = field(default=0, repr=False)
    _active: bool = field(default=True, repr=False)


_attempt_observation: ContextVar[HttpAttemptObservation | None] = ContextVar(
    "maimemo_http_attempt_observation",
    default=None,
)


@contextmanager
def observe_http_attempts() -> Iterator[HttpAttemptObservation]:
    observation = HttpAttemptObservation(_owner_task_id=id(asyncio.current_task()))
    token = _attempt_observation.set(observation)
    try:
        yield observation
    finally:
        observation._active = False
        _attempt_observation.reset(token)


def _observe_send() -> None:
    observation = _attempt_observation.get()
    if (
        observation is not None
        and observation._active
        and observation._owner_task_id == id(asyncio.current_task())
    ):
        observation.count += 1


class Limiter(Protocol):
    async def acquire(self, token_fingerprint: str) -> None: ...


class MaimemoTransport:
    def __init__(
        self,
        token: SecretStr,
        fingerprint_key: SecretStr,
        limiter: Limiter,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        max_attempts: int = 3,
    ) -> None:
        # Validate without encoding: UnicodeEncodeError retains the complete input.
        # Bearer credentials cannot contain whitespace or HTTP control characters.
        if not token.get_secret_value() or any(
            not 33 <= ord(character) <= 126 for character in token.get_secret_value()
        ):
            raise AuthenticationError("Invalid API credential")
        if not fingerprint_key.get_secret_value().strip():
            raise ValueError("Fingerprint key must not be empty")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._token = token
        self._fingerprint = hmac.new(
            fingerprint_key.get_secret_value().encode(),
            token.get_secret_value().encode(),
            hashlib.sha256,
        ).hexdigest()
        self._limiter = limiter
        self._client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=False)
        self._owns_client = client is None
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._max_attempts = max_attempts

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _retry_delay(self, response: httpx.Response | None, attempt: int) -> float:
        if response is not None and response.status_code == 429:
            value = response.headers.get("Retry-After")
            if value:
                try:
                    delay = float(value)
                    if delay >= 0 and delay < float("inf"):
                        return delay
                except ValueError:
                    pass
                try:
                    return max(0.0, (parsedate_to_datetime(value) - self._clock()).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
        return float(2**attempt) + self._jitter()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        response_type: type[T],
    ) -> T:
        parts = urlsplit(path)
        if (
            not path.startswith("/api/")
            or parts.scheme
            or parts.netloc
            or parts.query
            or parts.fragment
        ):
            raise InvalidRequestError("API path must be a relative /api/ path")
        for attempt in range(self._max_attempts):
            await self._limiter.acquire(self._fingerprint)
            response = None
            try:
                request = self._client.build_request(
                    method,
                    BASE_URL + path,
                    params=params,
                    json=json,
                    headers={"Authorization": f"Bearer {self._token.get_secret_value()}"},
                )
                _observe_send()
                response = await self._client.send(request, follow_redirects=False)
            except httpx.TransportError:
                pass
            # Raise only outside exception handlers: no sensitive exception context.
            if response is None or response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 == self._max_attempts:
                    if response is not None and response.status_code == 429:
                        raise RateLimitError("Upstream rate limit retry budget exhausted")
                    raise UpstreamUnavailableError("Upstream unavailable after bounded retries")
                await self._sleep(self._retry_delay(response, attempt))
                continue
            if response.status_code in (401, 403):
                raise AuthenticationError("API authentication rejected")
            if not 200 <= response.status_code < 300:
                raise InvalidRequestError("API request rejected")
            parsed = None
            try:
                parsed = response_type.model_validate(response.json())
            except (ValueError, ValidationError):
                pass
            if parsed is None:
                raise UpstreamSchemaError("API response failed schema validation")
            return parsed
        raise AssertionError("Unreachable retry state")
