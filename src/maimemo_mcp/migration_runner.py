"""Single-session, bounded startup migrations with fixed safe failure categories."""

import time
from pathlib import Path
from typing import Literal

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.pool import NullPool

from maimemo_mcp.config import Settings
from maimemo_mcp.database_url import parse_database_url

_LOCK_KEY = 21745489128041807
MigrationFailure = Literal[
    "database_unavailable", "migration_lock_timeout", "migration_statement_timeout",
    "migration_permission_denied", "migration_failed",
]


class MigrationError(RuntimeError):
    def __init__(self, category: MigrationFailure) -> None:
        super().__init__(category)


def _upgrade_locked(connection: Connection, settings: Settings, config: Config) -> None:
    acquired = False
    deadline = time.monotonic() + settings.migration_lock_timeout_seconds
    try:
        connection.execute(
            text("SELECT set_config('statement_timeout', :timeout, false)"),
            {"timeout": str(settings.migration_statement_timeout_seconds * 1000)},
        )
        connection.commit()
        while True:
            acquired = bool(connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": _LOCK_KEY},
            ))
            connection.commit()
            if acquired:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MigrationError("migration_lock_timeout")
            time.sleep(min(0.1, remaining))
        # Alembic owns no second connection; DDL and version writes share this transaction.
        config.attributes["connection"] = connection
        with connection.begin():
            command.upgrade(config, "head")
    finally:
        if acquired:
            # Failed DDL aborts a PostgreSQL transaction; rollback before unlocking.
            connection.rollback()
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _LOCK_KEY})
            connection.commit()


def run_upgrade(settings: Settings, config_path: Path = Path("alembic.ini")) -> None:
    engine: Engine | None = None
    failure: MigrationFailure | None = None
    try:
        if config_path == Path("alembic.ini"):
            config_path = Path(__file__).resolve().parents[2] / "alembic.ini"
        config = Config(str(config_path))
        engine = create_engine(
            parse_database_url(settings.database_url, required_driver="postgresql+psycopg"),
            poolclass=NullPool,
            connect_args={
                "options": "-c timezone=UTC",
                "connect_timeout": settings.database_connect_timeout_seconds,
            },
        )
        with engine.connect() as connection:
            _upgrade_locked(connection, settings, config)
    except MigrationError as exc:
        failure = "migration_lock_timeout" if str(exc) == "migration_lock_timeout" else (
            "migration_failed"
        )
    except DBAPIError as exc:
        state = getattr(exc.orig, "sqlstate", None)
        if state == "42501":
            failure = "migration_permission_denied"
        elif state == "57014":
            failure = "migration_statement_timeout"
        elif isinstance(exc, OperationalError):
            failure = "database_unavailable"
        else:
            failure = "migration_failed"
    except Exception:
        failure = "migration_failed"
    finally:
        if engine is not None:
            engine.dispose()
    # Leave the exception handler before raising to discard driver/SQL/URL context.
    if failure is not None:
        raise MigrationError(failure) from None
