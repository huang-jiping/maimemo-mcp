"""Real PostgreSQL rolling reservations and independent concurrent instances."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from maimemo.api_client.rate_limit import SharedRateLimiter
from maimemo.storage.database import create_session_factory
from maimemo.storage.models.rate_limit import ApiRateLimitWindow
from sqlalchemy import func, select, text

NOW = datetime(2026, 10, 2, tzinfo=UTC)
FINGERPRINT = "a" * 64


@pytest.mark.parametrize("limit,spacing,window", [(20, 0, 10), (40, 0.5, 60), (2000, 8, 18000)])
async def test_rolling_window_blocks_and_expires_at_exact_boundary(
    database, limit, spacing, window
):
    limiter = SharedRateLimiter(create_session_factory(database))
    for index in range(limit):
        assert (
            await limiter.reserve(FINGERPRINT, NOW + timedelta(seconds=index * spacing))
        ).allowed
    last = NOW + timedelta(seconds=(limit - 1) * spacing)
    denied = await limiter.reserve(FINGERPRINT, last)
    assert not denied.allowed
    assert denied.retry_at == NOW + timedelta(seconds=window)
    before = await limiter.reserve(FINGERPRINT, NOW + timedelta(seconds=window, microseconds=-1))
    assert not before.allowed
    assert (await limiter.reserve(FINGERPRINT, NOW + timedelta(seconds=window))).allowed


async def test_two_instances_share_limit_and_write_all_windows(database):
    sessions = create_session_factory(database)
    first, second = SharedRateLimiter(sessions), SharedRateLimiter(sessions)
    results = await asyncio.gather(
        *(limiter.reserve(FINGERPRINT, NOW) for limiter in [first, second] * 11)
    )
    assert sum(result.allowed for result in results) == 20
    async with sessions() as session:
        rows = (
            await session.execute(
                select(
                    ApiRateLimitWindow.window_type, func.sum(ApiRateLimitWindow.used_requests)
                ).group_by(ApiRateLimitWindow.window_type)
            )
        ).all()
        assert dict(rows) == {"10s": 20, "60s": 20, "5h": 20}
        expiries = (
            await session.execute(
                select(ApiRateLimitWindow.window_type, ApiRateLimitWindow.updated_at)
            )
        ).all()
        assert dict(expiries) == {
            "10s": NOW + timedelta(seconds=10),
            "60s": NOW + timedelta(seconds=60),
            "5h": NOW + timedelta(hours=5),
        }
    assert (await second.reserve("b" * 64, NOW)).allowed


async def test_acquire_waits_until_database_reservation_available(database):
    current = NOW
    waits = []

    async def sleep(delay):
        nonlocal current
        waits.append(delay)
        current += timedelta(seconds=delay)

    limiter = SharedRateLimiter(
        create_session_factory(database), clock=lambda: current, sleep=sleep
    )
    for _ in range(20):
        await limiter.acquire(FINGERPRINT)
    await limiter.acquire(FINGERPRINT)
    assert waits == [10.0]


async def test_crossing_aligned_bucket_boundary_does_not_reset_rolling_limit(database):
    limiter = SharedRateLimiter(create_session_factory(database))
    for _ in range(20):
        assert (await limiter.reserve(FINGERPRINT, NOW + timedelta(seconds=9))).allowed
    decision = await limiter.reserve(FINGERPRINT, NOW + timedelta(seconds=10))
    assert not decision.allowed
    assert decision.retry_at == NOW + timedelta(seconds=19)


async def test_acquire_reservation_starts_when_database_lock_is_obtained(database):
    sessions = create_session_factory(database)
    current = NOW
    limiter = SharedRateLimiter(sessions, clock=lambda: current)
    waiting = None
    try:
        async with sessions.begin() as holder:
            # Signed prefix of the synthetic fingerprint, hand-derived independently.
            await holder.execute(text("SELECT pg_advisory_xact_lock(-6148914691236517206)"))
            waiting = asyncio.create_task(limiter.acquire(FINGERPRINT))
            async with asyncio.timeout(5):
                async with sessions() as observer:
                    while not await observer.scalar(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_locks "
                                "WHERE locktype = 'advisory' AND NOT granted "
                                "AND classid = '2863311530'::oid "
                                "AND objid = '2863311530'::oid AND objsubid = 1)"
                        )
                    ):
                        await asyncio.sleep(0.01)
            # The actual database transaction is blocked, not a mocked call.
            current = NOW + timedelta(seconds=11)
        await asyncio.wait_for(waiting, timeout=5)
        async with sessions() as session:
            first_expiry = await session.scalar(
                select(ApiRateLimitWindow.updated_at).where(ApiRateLimitWindow.window_type == "10s")
            )
        assert first_expiry == NOW + timedelta(seconds=21)
        for _ in range(19):
            assert (await limiter.reserve(FINGERPRINT, current)).allowed
        twenty_first = await limiter.reserve(FINGERPRINT, current)
        assert not twenty_first.allowed
        assert twenty_first.retry_at == NOW + timedelta(seconds=21)
    finally:
        if waiting is not None and not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
