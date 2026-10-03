"""The standalone migration service keeps locking and errors bounded."""

import traceback

import pytest
from alembic import command
from maimemo.config import DatabaseSettings
from maimemo_server.config import MigrationSettings
from maimemo_server.migration_runner import MigrationError, run_upgrade
from sqlalchemy import create_engine, text

LOCK_KEY = 21745489128041807


def settings(postgres_url: str, **updates: int) -> MigrationSettings:
    return MigrationSettings(
        database=DatabaseSettings(database_url=postgres_url),
        **updates,
    )


def test_repeat_upgrade_preserves_current_database_and_releases_lock(
    postgres_url: str,
) -> None:
    run_upgrade(settings(postgres_url))
    run_upgrade(settings(postgres_url))
    engine = create_engine(postgres_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0004"
            assert connection.scalar(text("SELECT schema_version FROM schema_metadata")) == "0004"
            assert connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}
            )
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
    finally:
        engine.dispose()

def test_lock_timeout_is_safe_and_does_not_run_migration(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    try:
        with engine.connect() as blocker:
            blocker.execute(text("SELECT pg_advisory_lock(:key)"), {"key": LOCK_KEY})
            with pytest.raises(MigrationError, match="^migration_lock_timeout$"):
                run_upgrade(settings(postgres_url, lock_timeout_seconds=1))
            blocker.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
    finally:
        engine.dispose()


def test_migration_exception_is_redacted_and_lock_is_released(
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("SYNTHETIC_TOKEN postgresql://SYNTHETIC_PASSWORD@private")

    monkeypatch.setattr(command, "upgrade", fail)
    with pytest.raises(MigrationError, match="^migration_failed$") as caught:
        run_upgrade(settings(postgres_url))
    rendered = "".join(traceback.format_exception(caught.value))
    assert "SYNTHETIC" not in rendered
    assert caught.value.__context__ is None

    engine = create_engine(postgres_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}
            )
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
    finally:
        engine.dispose()
