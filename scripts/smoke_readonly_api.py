"""Fail-closed real-account smoke test for the 17 approved upstream reads.

This deliberately uses a fixed operation table instead of MCP discovery. Results and
errors are discarded; stdout contains only operation status, latency, and record count.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
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

_ARGUMENTS: dict[str, dict[str, Any]] = {
    "list_markji_folders": {},
    "list_markji_decks": {"request": {}},
    "get_markji_deck": {},
    "list_markji_chapters": {},
    "get_markji_chapter": {},
    "get_markji_card": {},
    "query_markji_files": {"request": {}},
    "get_interpretations": {},
    "get_notes": {},
    "list_notepads": {"request": {}},
    "get_notepad": {},
    "get_phrases": {},
    "get_study_progress": {},
    "get_today_items": {"request": {}},
    "query_study_records": {"request": {}},
    "get_vocabulary": {"spelling": "example"},
    "query_vocabulary": {"request": {"spellings": ["example"]}},
}
_RESOURCE_PATHS: dict[str, dict[str, tuple[str, ...]]] = {
    "get_markji_deck": {"deck": ("deck",)},
    "list_markji_chapters": {"deck": ("deck",)},
    "get_markji_chapter": {"deck": ("deck",), "chapter": ("chapter",)},
    "get_markji_card": {"deck": ("deck",), "card": ("card",)},
    "query_markji_files": {"file_id": ("request", "ids")},
    "get_interpretations": {"voc_id": ("voc_id",)},
    "get_notes": {"voc_id": ("voc_id",)},
    "get_notepad": {"notepad_id": ("notepad_id",)},
    "get_phrases": {"voc_id": ("voc_id",)},
}


@dataclass(frozen=True)
class Arguments:
    operations: tuple[str, ...]
    resource_ids: dict[tuple[str, str], str]


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
    parser.add_argument(
        "--resource-id",
        action="append",
        default=[],
        metavar="OPERATION.FIELD=VALUE",
        help=(
            "Supply an account resource identifier required by an ID lookup; repeat for "
            "multi-ID operations. Values are never printed."
        ),
    )
    namespace = parser.parse_args(argv)
    selected = tuple(namespace.operation or sorted(READ_ONLY_OPERATIONS))
    # Keep the runtime check independent from argparse choices and future parser edits.
    if not selected or any(name not in READ_ONLY_OPERATIONS for name in selected):
        parser.error("operation is outside the fixed 17-operation read-only allowlist")
    resource_ids: dict[tuple[str, str], str] = {}
    for item in namespace.resource_id:
        key, separator, value = item.partition("=")
        operation, dot, field = key.rpartition(".")
        pair = (operation, field)
        if (
            not separator
            or not dot
            or operation not in _RESOURCE_PATHS
            or field not in _RESOURCE_PATHS[operation]
        ):
            parser.error("resource ID must name an approved OPERATION.FIELD")
        if operation not in selected:
            parser.error("resource ID operation must also be selected")
        invalid_value = (
            not value.strip()
            or len(value) > 1000
            or any(ord(character) < 32 for character in value)
        )
        if invalid_value:
            parser.error("resource ID value must be 1..1000 printable characters")
        if pair in resource_ids:
            parser.error("resource ID must not be supplied more than once")
        resource_ids[pair] = value
    return Arguments(operations=selected, resource_ids=resource_ids)


def _operation_arguments(
    name: str, resource_ids: dict[tuple[str, str], str]
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    arguments = copy.deepcopy(_ARGUMENTS[name])
    fields = _RESOURCE_PATHS.get(name, {})
    missing = tuple(field for field in fields if (name, field) not in resource_ids)
    if missing:
        return None, missing
    for field, path in fields.items():
        target = arguments
        for component in path[:-1]:
            target = target[component]
        value: object = resource_ids[(name, field)]
        if path == ("request", "ids"):
            value = [value]
        target[path[-1]] = value
    return arguments, ()


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
    prerequisites = 0
    server = create_mcp_app(settings)
    async with Client(server.sdk) as client:
        for name in arguments.operations:
            operation_arguments, missing = _operation_arguments(name, arguments.resource_ids)
            if operation_arguments is None:
                prerequisites += 1
                required = ",".join(missing)
                print(
                    f"operation={name} status=PREREQUISITE latency_ms=0 count=0 "
                    f"required_fields={required}"
                )
                continue
            started = time.perf_counter()
            try:
                result = await client.call_tool(name, operation_arguments)
            except Exception:
                # Never render exception text: upstream/SDK errors may retain response bodies.
                result = None
            latency_ms = round((time.perf_counter() - started) * 1000)
            count = _record_count(result.structured_content) if result is not None else 0
            if result is None or result.is_error:
                failures += 1
                status = "FAIL"
            else:
                status = "PASS"
            print(f"operation={name} status={status} latency_ms={latency_ms} count={count}")
    print(
        f"summary status={'PASS' if failures == 0 and prerequisites == 0 else 'FAIL'} "
        f"operations={len(arguments.operations)} failures={failures} "
        f"prerequisites={prerequisites}"
    )
    return 0 if failures == 0 and prerequisites == 0 else 1


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
