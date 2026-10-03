"""Startup migration failures are isolated to disposable PostgreSQL schemas."""

import threading
import time
import traceback
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from maimemo_mcp import migration_runner, runtime
from maimemo_mcp.config import Settings

LOCK_KEY = 21745489128041807


@pytest.fixture
def startup_database(
    postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Settings, Engine, str]]:
    namespace = f"startup_{uuid4().hex}"
    admin = create_engine(postgres_url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{namespace}"'))
    options = f"-c timezone=UTC -c search_path={namespace}"
    probe = create_engine(postgres_url, connect_args={"options": options})

    def scoped_engine(*args: object, **kwargs: object) -> Engine:
        connect_args = dict(kwargs.get("connect_args", {}))
        assert connect_args["connect_timeout"] == 10
        connect_args["options"] = options
        return create_engine(*args, **{**kwargs, "connect_args": connect_args})

    monkeypatch.setattr(migration_runner, "create_engine", scoped_engine)
    token, key = tmp_path / "token", tmp_path / "key"
    token.write_text("SYNTHETIC_TOKEN", encoding="utf-8")
    key.write_text("SYNTHETIC_KEY", encoding="utf-8")
    settings = Settings(database_url=postgres_url, token_file=token,
                        token_fingerprint_key_file=key)
    try:
        yield settings, probe, namespace
    finally:
        probe.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{namespace}" CASCADE'))
        admin.dispose()


def assert_head(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0004"
        assert connection.scalar(text("SELECT schema_version FROM schema_metadata")) == "0004"


def assert_lock_released(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY})
        connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})


def test_empty_database_first_start_and_repeat_start_preserve_data(
    startup_database: tuple[Settings, Engine, str],
) -> None:
    settings, engine, _ = startup_database
    migration_runner.run_upgrade(settings)
    assert_head(engine)
    identity = uuid4()
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO vocabulary (id, maimemo_id, normalized_spelling, spelling) "
            "VALUES (:id, '123', 'apple', 'Apple')"
        ), {"id": identity})
    migration_runner.run_upgrade(settings)
    assert_head(engine)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT id FROM vocabulary")) == identity
    assert_lock_released(engine)


def test_upgrade_previous_revision_uses_injected_connection(
    startup_database: tuple[Settings, Engine, str],
) -> None:
    settings, engine, _ = startup_database
    config = Config(str(Path("alembic.ini").resolve()))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0003")
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0003"
    migration_runner.run_upgrade(settings)
    assert_head(engine)


def test_two_concurrent_attempts_serialize_on_migration_connection(
    startup_database: tuple[Settings, Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, engine, _ = startup_database
    real_upgrade = command.upgrade
    active = maximum = 0
    mutex = threading.Lock()
    barrier = threading.Barrier(2)
    sessions: list[int] = []

    def tracked_upgrade(config: Config, revision: str) -> None:
        nonlocal active, maximum
        connection = config.attributes["connection"]
        assert isinstance(connection, Connection)
        with mutex:
            active += 1
            maximum = max(active, maximum)
        try:
            sessions.append(connection.scalar(text("SELECT pg_backend_pid()")))
            assert connection.scalar(text(
                "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                "AND pid=pg_backend_pid() AND granted AND classid=:high AND objid=:low)"
            ), {"high": LOCK_KEY >> 32, "low": LOCK_KEY & 0xFFFFFFFF})
            time.sleep(0.15)
            real_upgrade(config, revision)
            assert connection.scalar(text("SELECT pg_backend_pid()")) == sessions[-1]
        finally:
            with mutex:
                active -= 1

    def attempt() -> None:
        barrier.wait(timeout=5)
        migration_runner.run_upgrade(settings)

    monkeypatch.setattr(command, "upgrade", tracked_upgrade)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(attempt) for _ in range(2)]
        for future in futures:
            future.result(timeout=20)
    assert maximum == 1
    assert len(sessions) == 2
    assert_head(engine)
    assert_lock_released(engine)


def test_advisory_lock_timeout_prevents_mcp_listen(
    startup_database: tuple[Settings, Engine, str], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings, engine, _ = startup_database
    settings = settings.model_copy(update={"migration_lock_timeout_seconds": 1})
    monkeypatch.setattr(Settings, "load", lambda: settings)
    monkeypatch.setattr(runtime, "configure_logging", lambda settings: None)
    serve = Mock()
    monkeypatch.setattr(runtime.uvicorn, "run", serve)
    with engine.connect() as blocker:
        blocker.execute(text("SELECT pg_advisory_lock(:key)"), {"key": LOCK_KEY})
        started = time.monotonic()
        try:
            assert runtime.main(["mcp"]) != 0
        finally:
            blocker.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
        assert 0.9 <= time.monotonic() - started < 5
    assert not serve.called
    assert capsys.readouterr().err == "startup_error migration_lock_timeout\n"
    assert_lock_released(engine)


def test_ddl_permission_failure_is_safe_and_releases_lock(
    startup_database: tuple[Settings, Engine, str], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings, engine, namespace = startup_database
    role = f"startup_no_ddl_{uuid4().hex}"
    admin = create_engine(settings.database_url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
        connection.execute(text(f'GRANT USAGE ON SCHEMA "{namespace}" TO "{role}"'))

    def restricted_engine(*args: object, **kwargs: object) -> Engine:
        options = f"-c timezone=UTC -c search_path={namespace} -c role={role}"
        return create_engine(*args, **{
            **kwargs, "connect_args": {"options": options, "connect_timeout": 10},
        })

    monkeypatch.setattr(migration_runner, "create_engine", restricted_engine)
    monkeypatch.setattr(Settings, "load", lambda: settings)
    monkeypatch.setattr(runtime, "configure_logging", lambda settings: None)
    serve = Mock()
    monkeypatch.setattr(runtime.uvicorn, "run", serve)
    try:
        assert runtime.main(["mcp"]) != 0
        assert not serve.called
        assert capsys.readouterr().err == "startup_error migration_permission_denied\n"
        assert_lock_released(engine)
    finally:
        with admin.begin() as connection:
            connection.execute(text(f'REVOKE USAGE ON SCHEMA "{namespace}" FROM "{role}"'))
            connection.execute(text(f'DROP ROLE "{role}"'))
        admin.dispose()


def test_statement_timeout_rolls_back_and_releases_lock(
    startup_database: tuple[Settings, Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, engine, _ = startup_database
    settings = settings.model_copy(update={"migration_statement_timeout_seconds": 1})

    def slow_upgrade(config: Config, revision: str) -> None:
        connection = config.attributes["connection"]
        connection.execute(text("CREATE TABLE must_roll_back (value int)"))
        connection.execute(text("SELECT pg_sleep(3)"))

    monkeypatch.setattr(command, "upgrade", slow_upgrade)
    started = time.monotonic()
    with pytest.raises(migration_runner.MigrationError, match="^migration_statement_timeout$"):
        migration_runner.run_upgrade(settings)
    assert time.monotonic() - started < 3
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT to_regclass('must_roll_back')")) is None
    assert_lock_released(engine)


def test_migration_exception_is_redacted_and_releases_lock(
    startup_database: tuple[Settings, Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, engine, _ = startup_database

    def broken_upgrade(config: Config, revision: str) -> None:
        raise RuntimeError("SYNTHETIC_TOKEN postgresql://SYNTHETIC_PASSWORD@private")

    monkeypatch.setattr(command, "upgrade", broken_upgrade)
    with pytest.raises(migration_runner.MigrationError, match="^migration_failed$") as caught:
        migration_runner.run_upgrade(settings)
    assert "SYNTHETIC" not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__context__ is None
    assert_lock_released(engine)


@pytest.mark.parametrize("mode", ["mcp", "worker"])
def test_database_unavailable_exits_without_service_or_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], mode: str,
) -> None:
    token, key = tmp_path / "token", tmp_path / "key"
    token.write_text("SYNTHETIC_TOKEN", encoding="utf-8")
    key.write_text("SYNTHETIC_KEY", encoding="utf-8")
    settings = Settings(
        database_url="postgresql+psycopg://u:SYNTHETIC_PASSWORD@127.0.0.1:1/unused_test",
        token_file=token, token_fingerprint_key_file=key,
        database_connect_timeout_seconds=1, schema_wait_timeout_seconds=1,
    )
    monkeypatch.setattr(Settings, "load", lambda: settings)
    monkeypatch.setattr(runtime, "configure_logging", lambda settings: None)
    serve, collect = Mock(), Mock()
    monkeypatch.setattr(runtime.uvicorn, "run", serve)
    monkeypatch.setattr(runtime.Worker, "run_forever", collect)
    started = time.monotonic()
    assert runtime.main([mode]) != 0
    assert time.monotonic() - started < 5
    assert not serve.called and not collect.called
    output = capsys.readouterr()
    assert output.out == ""
    expected = "database_unavailable" if mode == "mcp" else "schema_wait_timeout"
    assert output.err == f"startup_error {expected}\n"


@pytest.mark.parametrize(("alembic", "metadata"), [
    (None, None), ("0003", "0003"), ("9999", "9999"), ("0004", "0003"),
])
def test_worker_rejects_real_incompatible_schema_without_collecting(
    startup_database: tuple[Settings, Engine, str], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], alembic: str | None, metadata: str | None,
) -> None:
    settings, engine, namespace = startup_database
    settings = settings.model_copy(update={"schema_wait_timeout_seconds": 1})
    if alembic is not None:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num varchar)"))
            connection.execute(text("INSERT INTO alembic_version VALUES (:revision)"),
                               {"revision": alembic})
            connection.execute(text("CREATE TABLE schema_metadata (schema_version varchar)"))
            connection.execute(text("INSERT INTO schema_metadata VALUES (:revision)"),
                               {"revision": metadata})
    monkeypatch.setattr(Settings, "load", lambda: settings)
    monkeypatch.setattr(runtime, "configure_logging", lambda settings: None)
    monkeypatch.setattr(runtime, "create_async_engine_from_settings", lambda settings:
                        create_async_engine(settings.database_url, connect_args={
                            "options": f"-c search_path={namespace}", "connect_timeout": 1,
                        }))
    collect, migrate = AsyncMock(), Mock()
    monkeypatch.setattr(runtime.Worker, "run_forever", collect)
    monkeypatch.setattr(runtime, "run_upgrade", migrate)
    assert runtime.main(["worker"]) != 0
    assert not collect.called and not migrate.called
    assert capsys.readouterr().err == "startup_error schema_wait_timeout\n"


def test_mcp_real_empty_schema_migrates_before_http_startup(
    startup_database: tuple[Settings, Engine, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, engine, namespace = startup_database
    monkeypatch.setattr(Settings, "load", lambda: settings)
    monkeypatch.setattr(runtime, "configure_logging", lambda settings: None)
    monkeypatch.setattr(runtime, "create_async_engine_from_settings", lambda settings:
                        create_async_engine(settings.database_url, connect_args={
                            "options": f"-c search_path={namespace}", "connect_timeout": 10,
                        }))

    def serve(*args: object, **kwargs: object) -> None:
        assert_head(engine)

    monkeypatch.setattr(runtime.uvicorn, "run", serve)
    assert runtime.main(["mcp"]) == 0
