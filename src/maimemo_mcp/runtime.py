"""Production entrypoint for the MCP HTTP service and scheduled worker."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from typing import Literal, cast

import uvicorn

from maimemo_mcp.config import Settings
from maimemo_mcp.ingestion.scheduler import Schedule
from maimemo_mcp.ingestion.service import StudyIngestionService
from maimemo_mcp.ingestion.worker import Worker
from maimemo_mcp.logging import configure_logging
from maimemo_mcp.mcp_server.app import create_mcp_app
from maimemo_mcp.mcp_server.dependencies import open_dependencies

RuntimeMode = Literal["mcp", "worker"]


def parse_mode(argv: Sequence[str] | None = None) -> RuntimeMode:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("mcp", "worker"))
    return cast(RuntimeMode, parser.parse_args(argv).mode)


async def _run_worker(settings: Settings) -> None:
    async with open_dependencies(settings) as dependencies:
        service = StudyIngestionService(dependencies.sessions, dependencies.study)
        worker = Worker(service, Schedule(settings))
        await worker.run_forever()


def main(argv: Sequence[str] | None = None) -> int:
    mode = parse_mode(argv)
    settings = Settings.load()
    configure_logging(settings)
    if mode == "worker":
        asyncio.run(_run_worker(settings))
        return 0
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
