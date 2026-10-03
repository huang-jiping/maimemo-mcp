"""Standalone background Worker entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from maimemo.logging import configure_logging
from maimemo.storage.schema import SchemaStatus, expected_schema_revision, inspect_schema
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from maimemo_worker.config import WorkerSettings
from maimemo_worker.dependencies import open_worker_dependencies
from maimemo_worker.drift_monitor import OpenApiDriftMonitor
from maimemo_worker.worker import Worker


class SchemaWaitTimeout(RuntimeError):
    """The database did not reach the shipped migration head in time."""


def _parse_arguments(argv: Sequence[str] | None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)


def _report_configuration_error(exc: ValidationError) -> None:
    allowed_fields = {
        "database_url",
        "timezone",
        "log_level",
        "token_file",
        "token_fingerprint_key_file",
        "today_interval_minutes",
        "records_interval_minutes",
    }
    errors: list[str] = []
    for error in exc.errors(include_input=False, include_context=False, include_url=False):
        location = next(
            (str(item) for item in reversed(error["loc"]) if str(item) in allowed_fields),
            "configuration",
        )
        category = "missing" if error["type"] == "missing" else "invalid"
        errors.append(f"{location}:{category}")
    print("configuration_error " + ",".join(errors), file=sys.stderr)


async def _wait_for_schema(settings: WorkerSettings, engine: AsyncEngine) -> None:
    expected = expected_schema_revision()
    async with asyncio.timeout(settings.schema_wait_timeout_seconds):
        while True:
            state = await inspect_schema(engine, expected)
            if state.status is SchemaStatus.CURRENT:
                return
            await asyncio.sleep(1)


async def _run(settings: WorkerSettings) -> None:
    async with open_worker_dependencies(settings) as dependencies:
        try:
            await _wait_for_schema(settings, dependencies.engine)
        except TimeoutError:
            raise SchemaWaitTimeout from None
        monitor = OpenApiDriftMonitor(
            settings.drift_state_file,
            pinned_file=settings.pinned_openapi_file,
            remote_url=str(settings.openapi_url),
        )
        async with asyncio.TaskGroup() as tasks:
            worker_task = tasks.create_task(
                Worker(dependencies.service, dependencies.schedule).run_forever()
            )
            monitor_task = tasks.create_task(monitor.run_forever())
            await worker_task
            monitor_task.cancel()


def main(argv: Sequence[str] | None = None) -> int:
    _parse_arguments(argv)
    try:
        settings = WorkerSettings.load()
    except ValidationError as exc:
        _report_configuration_error(exc)
        return 2
    configure_logging(settings.core.log_level)
    try:
        asyncio.run(_run(settings))
    except SchemaWaitTimeout:
        print("startup_error schema_wait_timeout", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
