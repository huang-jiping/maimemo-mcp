"""Production entrypoint for the MCP HTTP service and scheduled worker."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Coroutine, Sequence
from datetime import UTC, datetime
from typing import Literal, cast

import uvicorn
from pydantic import ValidationError

from maimemo_mcp.config import Settings
from maimemo_mcp.database_url import DatabaseUrlError, parse_database_url
from maimemo_mcp.ingestion.scheduler import Schedule
from maimemo_mcp.ingestion.service import StudyIngestionService
from maimemo_mcp.ingestion.worker import Worker
from maimemo_mcp.logging import configure_logging
from maimemo_mcp.mcp_server.app import create_mcp_app
from maimemo_mcp.mcp_server.dependencies import open_dependencies
from maimemo_mcp.migration_runner import MigrationError, run_upgrade
from maimemo_mcp.storage.database import create_async_engine_from_settings
from maimemo_mcp.storage.schema import (
    SchemaDefinitionError,
    SchemaNotReadyError,
    expected_schema_revision,
    require_current_schema,
)

RuntimeMode = Literal["mcp", "worker"]


class SchemaWaitTimeoutError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("schema_wait_timeout")


def parse_mode(argv: Sequence[str] | None = None) -> RuntimeMode:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("mcp", "worker"))
    return cast(RuntimeMode, parser.parse_args(argv).mode)


async def _run_worker(settings: Settings) -> None:
    engine = create_async_engine_from_settings(settings)
    try:
        expected = expected_schema_revision()
        try:
            # This is the cancellation decision deadline. Await the driver's bounded
            # cleanup, even if it finishes later, and never collect after expiration.
            async with asyncio.timeout(settings.schema_wait_timeout_seconds) as deadline:
                while not deadline.expired():
                    try:
                        await require_current_schema(engine, expected)
                        break
                    except SchemaNotReadyError:
                        # Cleanup can turn cancellation into a normal schema error.
                        # An expired timeout will not cancel a second probe.
                        if deadline.expired():
                            break
                        await asyncio.sleep(0.25)
        except TimeoutError:
            pass
        else:
            # A dependency may finish cleanup by suppressing CancelledError.
            if not deadline.expired():
                await _collect(settings)
                return
        raise SchemaWaitTimeoutError() from None
    finally:
        await engine.dispose()


async def _collect(settings: Settings) -> None:
    async with open_dependencies(settings) as dependencies:
        service = StudyIngestionService(
            dependencies.sessions, dependencies.study, weakness=dependencies.weakness,
            clock=lambda: datetime.now(UTC),
        )
        worker = Worker(service, Schedule(settings))
        await worker.run_forever()


async def _prepare_mcp(settings: Settings) -> None:
    await asyncio.to_thread(run_upgrade, settings)
    engine = create_async_engine_from_settings(settings)
    try:
        await require_current_schema(engine, expected_schema_revision())
    finally:
        await engine.dispose()


def _run_async(operation: Coroutine[object, object, None]) -> None:
    if sys.platform == "win32":
        asyncio.run(operation, loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(operation)


def main(argv: Sequence[str] | None = None) -> int:
    mode = parse_mode(argv)
    try:
        settings = Settings.load()
    except ValidationError as exc:
        # Never format the validation exception: its input/context can retain secrets.
        errors = []
        for error in exc.errors(include_input=False, include_context=False, include_url=False):
            field = error["loc"][0] if error["loc"] else "configuration"
            if field not in Settings.model_fields:
                field = "configuration"
            category = "missing" if error["type"] == "missing" else "invalid"
            errors.append(f"{field}:{category}")
        print("configuration_error " + ",".join(errors), file=sys.stderr)
        return 2
    try:
        parse_database_url(settings.database_url, required_driver="postgresql+psycopg")
    except DatabaseUrlError:
        print("configuration_error database_url:invalid", file=sys.stderr)
        return 2
    try:
        settings.read_maimemo_token()
        settings.read_token_fingerprint_key()
    except OSError:
        print("configuration_error secret:unreadable", file=sys.stderr)
        return 2
    except ValueError:
        print("configuration_error secret:invalid", file=sys.stderr)
        return 2
    try:
        configure_logging(settings)
        if mode == "worker":
            _run_async(_run_worker(settings))
            return 0
        _run_async(_prepare_mcp(settings))
        app = create_mcp_app(settings)
        uvicorn.run(
            app,
            host=settings.mcp_host,
            port=settings.mcp_port,
            log_config=None,
            access_log=False,
            lifespan="on",
            # Uvicorn creates its own loop, independently of startup preflight.
            loop="asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto",
        )
    except (
        MigrationError, SchemaDefinitionError, SchemaNotReadyError, SchemaWaitTimeoutError,
    ) as exc:
        print(f"startup_error {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("startup_error startup_failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
