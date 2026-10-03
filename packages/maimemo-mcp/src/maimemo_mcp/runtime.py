"""Standalone MCP HTTP service entrypoint."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

import uvicorn
from maimemo.database_url import DatabaseUrlError
from maimemo.logging import configure_logging
from maimemo.storage.database import create_async_engine
from pydantic import ValidationError

from maimemo_mcp.config import MCPSettings
from maimemo_mcp.server import create_mcp_app


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
        "host",
        "port",
        "allowed_hosts",
        "drift_state_file",
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


def main(argv: Sequence[str] | None = None) -> int:
    _parse_arguments(argv)
    try:
        settings = MCPSettings.load()
    except ValidationError as exc:
        _report_configuration_error(exc)
        return 2
    try:
        engine = create_async_engine(
            settings.core.database.database_url,
            connect_timeout_seconds=settings.core.database.connect_timeout_seconds,
        )
        engine.sync_engine.dispose()
    except DatabaseUrlError:
        print("configuration_error database_url:invalid", file=sys.stderr)
        return 2
    configure_logging(settings.core.log_level)
    app = create_mcp_app(settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        lifespan="on",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
