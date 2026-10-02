"""PostgreSQL transactional rolling reservations shared by all API processes."""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maimemo_mcp.storage.models.rate_limit import ApiRateLimitWindow

WINDOWS = (("10s", 10, 20), ("60s", 60, 40), ("5h", 18000, 2000))


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_at: datetime | None = None


class SharedRateLimiter:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._sleep = sleep

    async def reserve(self, token_fingerprint: str, now: datetime) -> RateLimitDecision:
        return await self._reserve(token_fingerprint, now)

    async def _reserve(
        self, token_fingerprint: str, now: datetime | None = None
    ) -> RateLimitDecision:
        if not re.fullmatch(r"[0-9a-f]{64}", token_fingerprint):
            raise ValueError("Invalid token fingerprint")
        # A signed 64-bit lock namespace is stable across Python processes.
        lock_id = int.from_bytes(bytes.fromhex(token_fingerprint)[:8], "big", signed=True)
        async with self._sessions.begin() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id})
            # Production requests must not consume time while waiting for this lock.
            # Explicit reserve timestamps remain deterministic for callers/tests.
            now = self._clock() if now is None else now
            if now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("Reservation time must have a timezone")
            now = now.astimezone(UTC)
            await session.execute(
                delete(ApiRateLimitWindow).where(
                    ApiRateLimitWindow.token_hash == token_fingerprint,
                    ApiRateLimitWindow.updated_at <= now,
                )
            )
            retry_at = None
            for name, _, limit in WINDOWS:
                count, first_expiry = (
                    await session.execute(
                        select(
                            func.coalesce(func.sum(ApiRateLimitWindow.used_requests), 0),
                            func.min(ApiRateLimitWindow.updated_at),
                        ).where(
                            ApiRateLimitWindow.token_hash == token_fingerprint,
                            ApiRateLimitWindow.window_type == name,
                        )
                    )
                ).one()
                if count >= limit:
                    retry_at = max(retry_at or first_expiry, first_expiry)
            if retry_at is not None:
                return RateLimitDecision(False, retry_at)
            for name, seconds, _ in WINDOWS:
                statement = insert(ApiRateLimitWindow).values(
                    token_hash=token_fingerprint,
                    window_type=name,
                    window_start=now,
                    updated_at=now + timedelta(seconds=seconds),
                    used_requests=1,
                )
                # Equal injected/physical timestamps represent multiple reservations
                # with identical expiry, so their multiplicity is stored explicitly.
                await session.execute(
                    statement.on_conflict_do_update(
                        constraint="uq_api_rate_limit_window_identity",
                        set_={"used_requests": ApiRateLimitWindow.used_requests + 1},
                    )
                )
            return RateLimitDecision(True)

    async def acquire(self, token_fingerprint: str) -> None:
        while True:
            decision = await self._reserve(token_fingerprint)
            if decision.allowed:
                return
            assert decision.retry_at is not None
            await self._sleep(max(0.0, (decision.retry_at - self._clock()).total_seconds()))
