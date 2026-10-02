"""The real drift loop publishes safely, retries, and stops on cancellation."""

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from maimemo_mcp import openapi_drift as drift
from maimemo_mcp.ingestion.drift_monitor import OpenApiDriftMonitor
from maimemo_mcp.logging import SafeJsonFormatter

SPEC = b"openapi: 3.0.0\npaths: {}\n"
NOW = datetime(2026, 10, 2, tzinfo=UTC)


@pytest.mark.parametrize("failure", [None, "fetch", "parse", "write", "unexpected"])
async def test_immediate_check_six_hour_retry_and_safe_state(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    caplog.set_level("INFO")
    pinned, state = tmp_path / "pinned.yaml", tmp_path / "state.json"
    pinned.write_bytes(SPEC)
    attempts = 0
    waits: list[float] = []
    slept = asyncio.Event()
    release = asyncio.Event()
    write_state = drift._write_state

    def write(path: Path, report: drift.DriftReport, checked_at: datetime) -> None:
        if failure == "write" and attempts == 1:
            raise PermissionError("SYNTHETIC_PRIVATE_PATH")
        write_state(path, report, checked_at)

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if failure == "fetch":
                raise httpx.ConnectError("SYNTHETIC_PASSWORD body", request=request)
            if failure == "unexpected":
                raise RuntimeError("SYNTHETIC_PASSWORD body")
            if failure == "parse":
                return httpx.Response(200, content=b"SYNTHETIC_RESPONSE_BODY")
        return httpx.Response(200, content=SPEC)

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        slept.set()
        await release.wait()
        release.clear()
        slept.clear()

    monkeypatch.setattr(drift, "_write_state", write)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monitor = OpenApiDriftMonitor(
            state, pinned_file=pinned, client=client, sleep=sleep, clock=lambda: NOW,
        )
        task = asyncio.create_task(monitor.run_forever())
        try:
            await asyncio.wait_for(slept.wait(), 1)
            assert attempts == 1 and waits == [21600]
            assert state.exists() == (failure is None)
            release.set()
            while attempts < 2 or len(waits) < 2:
                await asyncio.sleep(0)
            saved = json.loads(state.read_text(encoding="utf-8"))
            assert set(saved) == {
                "severity", "checked_at", "pinned_sha256", "current_sha256",
            }
            assert saved["severity"] == "none"
            assert saved["checked_at"] == "2026-10-02T00:00:00Z"
            assert saved["pinned_sha256"] == saved["current_sha256"]
            assert waits == [21600, 21600]
            rendered = "\n".join(SafeJsonFormatter().format(r) for r in caplog.records)
            assert "SYNTHETIC" not in rendered
            assert str(tmp_path) not in rendered
            if failure:
                assert '"status":"failed"' in rendered
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)


async def test_cancellation_during_upstream_request_closes_stream_promptly(
    tmp_path: Path,
) -> None:
    started, closed = asyncio.Event(), asyncio.Event()
    pinned, state = tmp_path / "pinned.yaml", tmp_path / "state.json"
    pinned.write_bytes(SPEC)

    class BlockingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            started.set()
            await asyncio.Event().wait()
            yield SPEC

        async def aclose(self) -> None:
            closed.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=BlockingStream()),
    )) as client:
        task = asyncio.create_task(OpenApiDriftMonitor(
            state, pinned_file=pinned, client=client,
        ).run_forever())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert closed.is_set()
        assert not state.exists()


async def test_streamed_response_cannot_exceed_size_limit() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b" " * (drift.MAX_DOCUMENT_BYTES + 1)),
    )) as client:
        with pytest.raises(drift.SpecReadError, match="size_limit"):
            await drift.fetch_openapi(client, "https://example.test/spec")


async def test_cpu_check_is_isolated_and_cancellation_joins_its_owned_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    pinned, state = tmp_path / "pinned.yaml", tmp_path / "state.json"
    pinned.write_bytes(SPEC)
    started, heartbeat, finished = threading.Event(), threading.Event(), threading.Event()
    threads: list[int] = []
    main_thread = threading.get_ident()
    tasks_before = asyncio.all_tasks()
    real_compare = drift.compare_openapi

    def expensive_check(pinned: bytes, current: bytes, **kwargs: object) -> drift.DriftReport:
        threads.append(threading.get_ident())
        started.set()
        try:
            # The sampling heartbeat must run while CPU work is in progress.
            # A timeout makes the old synchronous implementation fail without hanging.
            assert heartbeat.wait(0.5)
            stop = kwargs.get("stop")
            assert isinstance(stop, threading.Event)
            assert stop.wait(0.5)
            # Preserve the actual parser's cancellation checkpoint, not a fake error.
            return real_compare(pinned, current, stop=stop)
        finally:
            finished.set()

    async def collection_heartbeat() -> None:
        while not started.is_set():
            await asyncio.sleep(0)
        heartbeat.set()

    monkeypatch.setattr(drift, "compare_openapi", expensive_check)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=SPEC),
    )) as client:
        task = asyncio.create_task(OpenApiDriftMonitor(
            state, pinned_file=pinned, client=client,
        ).run_forever())
        pulse = asyncio.create_task(collection_heartbeat())
        try:
            await asyncio.wait_for(pulse, 1)
            assert not finished.is_set(), "CPU work blocked the sampling heartbeat"
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            await pulse
        assert threads[0] != main_thread
        assert finished.is_set(), "monitor exited before its CPU task stopped"
        assert asyncio.all_tasks() == tasks_before
        assert not state.exists()
