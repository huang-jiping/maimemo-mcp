"""Container entrypoint mode selection is explicit and closed."""

import asyncio
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from maimemo_mcp import runtime
from maimemo_mcp.config import Settings
from maimemo_mcp.runtime import parse_mode
from maimemo_mcp.storage.schema import SchemaNotReadyError


def test_runtime_accepts_only_mcp_or_worker_modes() -> None:
    assert parse_mode(["mcp"]) == "mcp"
    assert parse_mode(["worker"]) == "worker"
    with pytest.raises(SystemExit):
        parse_mode(["shell"])


@pytest.mark.parametrize("invalid", [False, True])
def test_startup_configuration_failure_is_controlled_and_secret_free(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], invalid: bool,
) -> None:
    import os

    from maimemo_mcp.runtime import main

    for name in list(os.environ):
        if name.startswith("MAIMEMO_"):
            monkeypatch.delenv(name)
    url = "postgresql://u:SYNTHETIC_STARTUP_SECRET@h.invalid/d"
    monkeypatch.setenv("MAIMEMO_DATABASE_URL", url)
    if invalid:
        monkeypatch.setenv("MAIMEMO_TOKEN_FILE", "unused")
        monkeypatch.setenv("MAIMEMO_TOKEN_FINGERPRINT_KEY_FILE", "unused")
        monkeypatch.setenv("MAIMEMO_MCP_PORT", url)
    assert main(["worker"]) == 2
    output = capsys.readouterr()
    assert "SYNTHETIC_STARTUP_SECRET" not in output.err + output.out
    assert "postgresql://" not in output.err + output.out
    assert "configuration_error" in output.err


@pytest.mark.parametrize("mode", ["worker", "mcp"])
@pytest.mark.parametrize("private_url", [
    "postgresql+psycopg://u:prefix@host:LEAK@localhost/d",
    "postgresql+psycopg://u:prefix@host/d?port=LEAK@localhost/d",
    "postgresql+psycopg://u:prefix@LEAK@localhost/d",
])
def test_malformed_database_url_fails_before_runtime_without_secret_or_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
    private_url: str,
) -> None:
    import os

    from maimemo_mcp.runtime import main

    for name in list(os.environ):
        if name.startswith("MAIMEMO_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("MAIMEMO_DATABASE_URL", private_url)
    monkeypatch.setenv("MAIMEMO_TOKEN_FILE", "unused")
    monkeypatch.setenv("MAIMEMO_TOKEN_FINGERPRINT_KEY_FILE", "unused")

    assert main([mode]) == 2

    output = capsys.readouterr()
    combined = output.err + output.out
    assert combined == "configuration_error database_url:invalid\n"
    assert "LEAK" not in combined
    assert private_url not in combined
    assert "Traceback" not in combined


@pytest.mark.parametrize("mode", ["worker", "mcp"])
def test_runtime_preflight_controls_late_engine_url_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    from pathlib import Path

    from maimemo_mcp.config import Settings
    from maimemo_mcp.runtime import main

    settings = Settings.model_construct(
        database_url=(
            "postgresql+psycopg://u:prefix@host/d?port=LEAK@localhost/d"
        ),
        token_file=Path("unused"),
        token_fingerprint_key_file=Path("unused"),
    )
    monkeypatch.setattr(Settings, "load", lambda: settings)

    assert main([mode]) == 2

    output = capsys.readouterr()
    combined = output.err + output.out
    assert combined == "configuration_error database_url:invalid\n"
    assert "LEAK" not in combined
    assert "Traceback" not in combined


@pytest.fixture
def startup_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    token, key = tmp_path / "token", tmp_path / "key"
    token.write_text("SYNTHETIC_TOKEN", encoding="utf-8")
    key.write_text("SYNTHETIC_KEY", encoding="utf-8")
    settings = Settings(
        database_url="postgresql+psycopg://u:SYNTHETIC_DB_PASSWORD@localhost/d",
        token_file=token, token_fingerprint_key_file=key,
        schema_wait_timeout_seconds=1,
    )
    monkeypatch.setattr(Settings, "load", lambda: settings)
    monkeypatch.setattr(runtime, "configure_logging", lambda settings: None)
    return settings


@pytest.mark.parametrize("mode", ["mcp", "worker"])
@pytest.mark.parametrize("secret", ["token_file", "token_fingerprint_key_file"])
@pytest.mark.parametrize("failure", ["missing", "unreadable", "empty", "utf8"])
def test_secret_failure_prevents_all_database_and_service_work(
    startup_settings: Settings, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], mode: str, secret: str, failure: str,
) -> None:
    path = getattr(startup_settings, secret)
    if failure == "missing":
        path.unlink()
    elif failure == "empty":
        path.write_text(" \n", encoding="utf-8")
    elif failure == "utf8":
        path.write_bytes(b"SYNTHETIC_SECRET_BYTES\xff")
    else:
        read_text = Path.read_text

        def denied(target: Path, **kwargs: object) -> str:
            if target == path:
                raise PermissionError("SYNTHETIC_PRIVATE_PATH")
            return read_text(target, **kwargs)

        monkeypatch.setattr(Path, "read_text", denied)
    migrate = Mock()
    serve = Mock()
    database = Mock(side_effect=AssertionError("secret gate ran too late"))
    monkeypatch.setattr(runtime, "run_upgrade", migrate, raising=False)
    monkeypatch.setattr(runtime.uvicorn, "run", serve)
    monkeypatch.setattr(runtime, "create_async_engine_from_settings", database)
    assert runtime.main([mode]) != 0
    assert not migrate.called and not serve.called and not database.called
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith("configuration_error secret:")
    assert "SYNTHETIC" not in output.err
    assert str(path) not in output.err
    assert "Traceback" not in output.err


def test_mcp_migrates_outside_event_loop_then_checks_schema_before_serving(
    startup_settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    main_thread = threading.get_ident()

    def upgrade(settings: Settings) -> None:
        assert settings is startup_settings
        assert threading.get_ident() != main_thread
        events.append("migration")

    async def require(engine: object, expected: str) -> None:
        assert expected == "0004"
        events.append("schema")

    def serve(*args: object, **kwargs: object) -> None:
        assert events == ["migration", "schema"]
        events.append("serve")

    monkeypatch.setattr(runtime, "run_upgrade", upgrade, raising=False)
    monkeypatch.setattr(runtime, "require_current_schema", require, raising=False)
    monkeypatch.setattr(runtime.uvicorn, "run", serve)
    assert runtime.main(["mcp"]) == 0
    assert events == ["migration", "schema", "serve"]


@pytest.mark.parametrize("stage", ["migration", "schema"])
def test_mcp_startup_failure_never_serves_or_leaks_exception_details(
    startup_settings: Settings, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], stage: str,
) -> None:
    def upgrade(settings: Settings) -> None:
        if stage == "migration":
            raise RuntimeError("SYNTHETIC_DB_PASSWORD postgresql://private")

    async def require(engine: object, expected: str) -> None:
        raise SchemaNotReadyError()

    serve = Mock()
    monkeypatch.setattr(runtime, "run_upgrade", upgrade, raising=False)
    monkeypatch.setattr(runtime, "require_current_schema", require, raising=False)
    monkeypatch.setattr(runtime.uvicorn, "run", serve)
    assert runtime.main(["mcp"]) != 0
    assert not serve.called
    output = capsys.readouterr()
    assert "SYNTHETIC" not in output.err + output.out
    assert "postgresql://" not in output.err + output.out
    assert "Traceback" not in output.err + output.out


@pytest.mark.parametrize("unavailable", [False, True])
def test_worker_timeout_prevents_collection(
    startup_settings: Settings, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], unavailable: bool,
) -> None:
    async def require(engine: object, expected: str) -> None:
        raise SchemaNotReadyError(unavailable=unavailable)

    collect = Mock()
    migrate = Mock(side_effect=AssertionError("worker must not migrate"))
    monkeypatch.setattr(runtime, "require_current_schema", require, raising=False)
    monkeypatch.setattr(runtime.Worker, "run_forever", collect)
    monkeypatch.setattr(runtime, "run_upgrade", migrate, raising=False)
    assert runtime.main(["worker"]) != 0
    assert not collect.called and not migrate.called
    assert "schema_wait_timeout" in capsys.readouterr().err


def test_worker_waits_for_current_schema_then_collects(
    startup_settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    async def require(engine: object, expected: str) -> None:
        assert expected == "0004"
        events.append("schema")
        if events == ["schema"]:
            raise SchemaNotReadyError()

    async def collect(worker: object) -> None:
        assert events == ["schema", "schema"]
        events.append("collect")

    monkeypatch.setattr(runtime, "require_current_schema", require, raising=False)
    monkeypatch.setattr(runtime.Worker, "run_forever", collect)
    assert runtime.main(["worker"]) == 0
    assert events == ["schema", "schema", "collect"]


def test_worker_schema_probe_is_cancelled_at_wait_deadline(
    startup_settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = False

    async def require(engine: object, expected: str) -> None:
        nonlocal cancelled
        try:
            await asyncio.sleep(60)
        finally:
            cancelled = True

    collect = Mock()
    monkeypatch.setattr(runtime, "require_current_schema", require, raising=False)
    monkeypatch.setattr(runtime.Worker, "run_forever", collect)
    assert runtime.main(["worker"]) != 0
    assert cancelled and not collect.called
