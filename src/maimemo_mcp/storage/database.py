"""Async PostgreSQL engine and session ownership boundaries."""

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from maimemo_mcp.config import Settings


def create_async_engine_from_settings(settings: Settings) -> AsyncEngine:
    url = make_url(settings.database_url)
    if url.drivername != "postgresql+psycopg":
        raise ValueError("database_url must use the postgresql+psycopg async dialect")
    return create_async_engine(
        url,
        pool_pre_ping=True,
        connect_args={"options": "-c timezone=UTC"},
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
