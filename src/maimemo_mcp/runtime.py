"""Production entrypoint for the MCP HTTP service."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Literal, cast

import uvicorn
from maimemo.database_url import DatabaseUrlError
from maimemo.logging import configure_logging
from maimemo.storage.database import create_async_engine
from pydantic import ValidationError

from maimemo_mcp.config import Settings
from maimemo_mcp.mcp_server.app import create_mcp_app

RuntimeMode = Literal["mcp"]


def parse_mode(argv: Sequence[str] | None = None) -> RuntimeMode:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("mcp",))
    return cast(RuntimeMode, parser.parse_args(argv).mode)


def main(argv: Sequence[str] | None = None) -> int:
    parse_mode(argv)
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
        engine = create_async_engine(settings.database_url)
        engine.sync_engine.dispose()
    except DatabaseUrlError:
        print("configuration_error database_url:invalid", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)
    app = create_mcp_app(settings)
    uvicorn.run(
        app,
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_config=None,
        access_log=False,
        lifespan="on",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
