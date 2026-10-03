"""Worker startup gates collection and owns the drift monitor lifecycle."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest


async def test_run_does_not_start_background_tasks_before_schema_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from maimemo_worker import runtime

    events: list[str] = []
    dependencies = SimpleNamespace(engine=object(), service=object(), schedule=object())

    @asynccontextmanager
    async def open_dependencies(settings: object) -> Any:
        yield dependencies

    async def reject_schema(settings: object, engine: object) -> None:
        events.append("schema")
        raise runtime.SchemaWaitTimeout

    class ForbiddenWorker:
        def __init__(self, service: object, schedule: object) -> None:
            raise AssertionError("worker started before schema gate")

    class ForbiddenMonitor:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("monitor started before schema gate")

    monkeypatch.setattr(runtime, "open_worker_dependencies", open_dependencies)
    monkeypatch.setattr(runtime, "_wait_for_schema", reject_schema)
    monkeypatch.setattr(runtime, "Worker", ForbiddenWorker)
    monkeypatch.setattr(runtime, "OpenApiDriftMonitor", ForbiddenMonitor)

    with pytest.raises(runtime.SchemaWaitTimeout):
        await runtime._run(object())

    assert events == ["schema"]


async def test_worker_completion_cancels_monitor_and_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from maimemo_worker import runtime

    monitor_started = asyncio.Event()
    monitor_stopped = asyncio.Event()
    dependencies = SimpleNamespace(engine=object(), service=object(), schedule=object())
    settings = SimpleNamespace(
        drift_state_file="state.json",
        pinned_openapi_file="pinned.yaml",
        openapi_url="https://example.invalid/openapi.yaml",
    )

    @asynccontextmanager
    async def open_dependencies(current_settings: object) -> Any:
        assert current_settings is settings
        yield dependencies

    async def schema_ready(current_settings: object, engine: object) -> None:
        assert current_settings is settings
        assert engine is dependencies.engine

    class CompletingWorker:
        def __init__(self, service: object, schedule: object) -> None:
            assert service is dependencies.service
            assert schedule is dependencies.schedule

        async def run_forever(self) -> None:
            await monitor_started.wait()

    class WaitingMonitor:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def run_forever(self) -> None:
            monitor_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                monitor_stopped.set()

    monkeypatch.setattr(runtime, "open_worker_dependencies", open_dependencies)
    monkeypatch.setattr(runtime, "_wait_for_schema", schema_ready)
    monkeypatch.setattr(runtime, "Worker", CompletingWorker)
    monkeypatch.setattr(runtime, "OpenApiDriftMonitor", WaitingMonitor)

    await asyncio.wait_for(runtime._run(settings), timeout=0.25)

    assert monitor_stopped.is_set()
