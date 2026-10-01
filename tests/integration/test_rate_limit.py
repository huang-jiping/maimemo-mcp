"""Real PostgreSQL rolling reservations and independent concurrent instances."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from maimemo_mcp.maimemo_client.rate_limit import SharedRateLimiter
from maimemo_mcp.storage.database import create_session_factory
from maimemo_mcp.storage.models.rate_limit import ApiRateLimitWindow

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
