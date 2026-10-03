"""Standalone minimal HTTP Server entrypoint."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

import uvicorn
from pydantic import ValidationError

from maimemo_server.app import create_app
from maimemo_server.config import ServerSettings


def _parse_arguments(argv: Sequence[str] | None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    _parse_arguments(argv)
    try:
        settings = ServerSettings.load()
    except ValidationError as exc:
        missing = any(error["type"] == "missing" for error in exc.errors())
        category = "missing" if missing else "invalid"
        print(f"configuration_error database_url:{category}", file=sys.stderr)
        return 2
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        lifespan="on",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
