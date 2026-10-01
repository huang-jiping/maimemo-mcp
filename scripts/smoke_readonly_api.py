"""Fail-closed real-account smoke test for the 17 approved upstream reads.

This deliberately uses a fixed operation table instead of MCP discovery. Results and
errors are discarded; stdout contains only operation status, latency, and record count.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from mcp.client import Client

from maimemo_mcp.config import Settings
from maimemo_mcp.mcp_server.app import create_mcp_app

READ_ONLY_OPERATIONS = {
    "list_markji_folders",
    "list_markji_decks",
    "get_markji_deck",
    "list_markji_chapters",
    "get_markji_chapter",
    "get_markji_card",
    "query_markji_files",
    "get_interpretations",
    "get_notes",
    "list_notepads",
    "get_notepad",
    "get_phrases",
    "get_study_progress",
    "get_today_items",
    "query_study_records",
    "get_vocabulary",
    "query_vocabulary",
}

_PLACEHOLDER = "__maimemo_readonly_smoke_missing_identifier__"
_ARGUMENTS: dict[str, dict[str, Any]] = {
    "list_markji_folders": {},
    "list_markji_decks": {"request": {}},
    "get_markji_deck": {"deck": _PLACEHOLDER},
    "list_markji_chapters": {"deck": _PLACEHOLDER},
    "get_markji_chapter": {"deck": _PLACEHOLDER, "chapter": _PLACEHOLDER},
    "get_markji_card": {"deck": _PLACEHOLDER, "card": _PLACEHOLDER},
    "query_markji_files": {"request": {"ids": [_PLACEHOLDER]}},
    "get_interpretations": {"voc_id": _PLACEHOLDER},
    "get_notes": {"voc_id": _PLACEHOLDER},
    "list_notepads": {"request": {}},
    "get_notepad": {"notepad_id": _PLACEHOLDER},
    "get_phrases": {"voc_id": _PLACEHOLDER},
    "get_study_progress": {},
    "get_today_items": {"request": {}},
    "query_study_records": {"request": {}},
    "get_vocabulary": {"spelling": "example"},
    "query_vocabulary": {"request": {"spellings": ["example"]}},
}
_IDENTIFIER_PREREQUISITE = {
    name for name, arguments in _ARGUMENTS.items() if _PLACEHOLDER in repr(arguments)
}


@dataclass(frozen=True)
class Arguments:
    operations: tuple[str, ...]


def parse_args(argv: Sequence[str] | None = None) -> Arguments:
    parser = argparse.ArgumentParser(
        description="Run only the 17 fixed Maimemo read operations without printing payloads."
    )
    parser.add_argument(
        "--confirm-readonly",
        action="store_true",
        required=True,
        help="Required acknowledgement that only the fixed read allowlist may run.",
    )
    parser.add_argument(
        "--operation",
        action="append",
        choices=sorted(READ_ONLY_OPERATIONS),
        help="Run a specific allowlisted operation; repeat as needed. Default: all 17.",
    )
    namespace = parser.parse_args(argv)
    selected = tuple(namespace.operation or sorted(READ_ONLY_OPERATIONS))
    # Keep the runtime check independent from argparse choices and future parser edits.
    if not selected or any(name not in READ_ONLY_OPERATIONS for name in selected):
        parser.error("operation is outside the fixed 17-operation read-only allowlist")
    return Arguments(operations=selected)


def _record_count(content: object) -> int:
    if not isinstance(content, dict):
        return 0
    data = content.get("data")
    if data is None:
        return 0
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        lists = [value for value in data.values() if isinstance(value, list)]
        return sum(len(value) for value in lists) if lists else 1
    return 1


async def run(arguments: Arguments) -> int:
    if set(_ARGUMENTS) != READ_ONLY_OPERATIONS:
        raise RuntimeError("Internal smoke table differs from the fixed read-only allowlist")
    settings = Settings.load()
    failures = 0
    server = create_mcp_app(settings)
    async with Client(server.sdk) as client:
        for name in arguments.operations:
            started = time.perf_counter()
            try:
                result = await client.call_tool(name, _ARGUMENTS[name])
            except Exception:
                # Never render exception text: upstream/SDK errors may retain response bodies.
                result = None
            latency_ms = round((time.perf_counter() - started) * 1000)
            count = _record_count(result.structured_content) if result is not None else 0
            if result is None or result.is_error:
                failures += 1
                status = "PREREQUISITE" if name in _IDENTIFIER_PREREQUISITE else "FAIL"
            else:
                status = "PASS"
            print(f"operation={name} status={status} latency_ms={latency_ms} count={count}")
    print(
        f"summary status={'PASS' if failures == 0 else 'FAIL'} "
        f"operations={len(arguments.operations)} failures={failures}"
    )
    return 0 if failures == 0 else 1


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        return asyncio.run(run(arguments))
    except Exception:
        # Fail closed without emitting settings, credentials, response bodies or exception text.
        print("summary status=FAIL operations=0 failures=1")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
