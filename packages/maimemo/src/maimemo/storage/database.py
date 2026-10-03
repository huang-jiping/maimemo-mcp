"""Async PostgreSQL engine and session ownership boundaries."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.ext.asyncio import (
    create_async_engine as _sqlalchemy_create_async_engine,
)

from maimemo.database_url import DatabaseUrlError, parse_database_url


def create_async_engine(
    database_url: str,
    *,
    connect_timeout_seconds: int = 10,
) -> AsyncEngine:
    try:
        url = parse_database_url(
            database_url, required_driver="postgresql+psycopg"
        )
        return _sqlalchemy_create_async_engine(
            url,
            pool_pre_ping=True,
            connect_args={
                "options": "-c timezone=UTC",
                "connect_timeout": connect_timeout_seconds,
            },
        )
    except Exception:
        # Raise after leaving the handler so dialect errors cannot retain a URL
        # value in __context__ or a formatted traceback.
        pass
    raise DatabaseUrlError("Invalid database URL") from None


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
