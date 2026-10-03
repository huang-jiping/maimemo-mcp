"""Standalone background Worker entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from maimemo.logging import configure_logging
from pydantic import ValidationError

from maimemo_worker.config import WorkerSettings
from maimemo_worker.dependencies import open_worker_dependencies
from maimemo_worker.worker import Worker


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


async def _run(settings: WorkerSettings) -> None:
    async with open_worker_dependencies(settings) as dependencies:
        await Worker(dependencies.service, dependencies.schedule).run_forever()


def main(argv: Sequence[str] | None = None) -> int:
    _parse_arguments(argv)
    try:
        settings = WorkerSettings.load()
    except ValidationError as exc:
        _report_configuration_error(exc)
        return 2
    configure_logging(settings.core.log_level)
    asyncio.run(_run(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
