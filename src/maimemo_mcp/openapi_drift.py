"""Compare the pinned OpenAPI contract with the public upstream document."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.request
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any, Literal

import httpx
import yaml

Severity = Literal["none", "informational", "high"]
HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_STRUCTURE_ITEMS = 20000
MAX_STRUCTURE_DEPTH = 64
MAX_WORK_ITEMS = 50000
# Count string expansion as well as nodes: a long parent repeated over many
# leaves can otherwise exhaust the Worker's 512 MiB limit from a small document.
MAX_SCHEMA_PATH_BYTES = 1024
MAX_EXPANDED_PATH_BYTES = 1024 * 1024
DEFAULT_PINNED_FILE = Path(__file__).resolve().parents[2] / "openapi" / "maimemo-api.yaml"
DEFAULT_REMOTE_URL = "https://open.maimemo.com/api_bundle.yaml"


class SpecReadError(ValueError):
    """The supplied bytes are not a readable OpenAPI mapping."""


@dataclass
class _WorkBudget:
    stop: Event | None = None
    remaining: int = MAX_WORK_ITEMS
    remaining_path_bytes: int = MAX_EXPANDED_PATH_BYTES

    def consume(self, count: int = 1) -> None:
        if self.stop is not None and self.stop.is_set():
            raise SpecReadError("check_cancelled")
        self.remaining -= count
        if self.remaining < 0:
            raise SpecReadError("work_limit")

    def reserve_path_bytes(self, count: int) -> None:
        self.consume(0)
        self.remaining_path_bytes -= count
        if self.remaining_path_bytes < 0:
            raise SpecReadError("path_bytes_limit")


_WORK_BUDGET: ContextVar[_WorkBudget | None] = ContextVar("drift_work_budget", default=None)


def _consume(count: int = 1) -> None:
    budget = _WORK_BUDGET.get()
    if budget is not None:
        budget.consume(count)


def _schema_path(prefix: str, name: str, *, separator: str = "") -> str:
    # Reject by character count before encoding, then measure UTF-8 before
    # allocating or retaining the combined path. Encoding touches at most 1024
    # characters per piece, even when a malicious input has a huge scalar key.
    parts = (prefix, separator, name)
    if sum(len(part) for part in parts) > MAX_SCHEMA_PATH_BYTES:
        raise SpecReadError("path_limit")
    try:
        size = sum(len(part.encode("utf-8")) for part in parts)
    except UnicodeError:
        raise SpecReadError("path_limit") from None
    if size > MAX_SCHEMA_PATH_BYTES:
        raise SpecReadError("path_limit")
    budget = _WORK_BUDGET.get()
    if budget is not None:
        budget.reserve_path_bytes(size)
    return f"{prefix}{separator}{name}"


def _parameter_path(parameter: Mapping[str, Any]) -> str:
    return _schema_path(
        str(parameter.get("in", "unknown")), str(parameter.get("name", "unknown")),
        separator=":",
    )


class _BoundedLoader(yaml.SafeLoader):
    """Bound composition before construction can expand YAML merge aliases."""

    def __init__(self, document: bytes) -> None:
        super().__init__(document)
        self._depth = 0

    def compose_node(self, parent: Any, index: Any) -> yaml.Node | None:
        _consume()
        self._depth += 1
        try:
            if self._depth > MAX_STRUCTURE_DEPTH:
                raise SpecReadError("structure_limit")
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1


def _validate_structure(
    node: yaml.Node, memo: dict[int, tuple[int, int]], active: set[int],
) -> tuple[int, int]:
    """Measure expanded DAG size without expanding it, rejecting cycles and alias bombs."""
    _consume()
    identity = id(node)
    if identity in active:
        raise SpecReadError("structure_limit")
    if identity in memo:
        return memo[identity]
    active.add(identity)
    size, depth = 1, 1
    children: list[yaml.Node] = []
    if isinstance(node, yaml.MappingNode):
        children = [child for pair in node.value for child in pair]
    elif isinstance(node, yaml.SequenceNode):
        children = node.value
    for child in children:
        child_size, child_depth = _validate_structure(child, memo, active)
        size += child_size
        depth = max(depth, child_depth + 1)
        if size > MAX_STRUCTURE_ITEMS or depth > MAX_STRUCTURE_DEPTH:
            raise SpecReadError("structure_limit")
    active.remove(identity)
    memo[identity] = size, depth
    return size, depth


@dataclass(frozen=True)
class DriftChange:
    severity: Literal["informational", "high"]
    kind: str
    operation_id: str
    detail: str


@dataclass(frozen=True)
class DriftReport:
    severity: Severity
    pinned_sha256: str
    current_sha256: str
    changes: tuple[DriftChange, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "pinned_sha256": self.pinned_sha256,
            "current_sha256": self.current_sha256,
            "changes": [asdict(change) for change in self.changes],
        }


@dataclass(frozen=True)
class Operation:
    operation_id: str
    method: str
    path: str
    required_inputs: frozenset[str]
    optional_inputs: frozenset[str]
    required_response_fields: frozenset[str]
    optional_response_fields: frozenset[str]


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SpecReadError(f"{label} must be a mapping")
    _consume(1 + len(value))
    return {str(key): item for key, item in value.items()}


def _load(document: bytes) -> Mapping[str, Any]:
    if len(document) > MAX_DOCUMENT_BYTES:
        raise SpecReadError("size_limit")
    try:
        loader = _BoundedLoader(document)
        try:
            node = loader.get_single_node()
            if node is None:
                parsed = None
            else:
                _validate_structure(node, {}, set())
                parsed = loader.construct_document(node)
        finally:
            loader.dispose()
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SpecReadError("OpenAPI document is not valid UTF-8 YAML") from exc
    root = _mapping(parsed, label="OpenAPI document")
    if not isinstance(root.get("openapi"), str):
        raise SpecReadError("OpenAPI document has no version")
    _mapping(root.get("paths"), label="OpenAPI paths")
    return root


def _resolve(root: Mapping[str, Any], schema: object) -> Mapping[str, Any]:
    value = _mapping(schema, label="schema")
    reference = value.get("$ref")
    if reference is None:
        return value
    if not isinstance(reference, str) or not reference.startswith("#/"):
        raise SpecReadError("Only local schema references are supported")
    target: object = root
    for part in reference[2:].split("/"):
        target = _mapping(target, label="referenced schema").get(
            part.replace("~1", "/").replace("~0", "~")
        )
    return _mapping(target, label=f"reference {reference}")


def _schema_fields(
    root: Mapping[str, Any],
    schema: object,
    *,
    prefix: str = "",
    ancestor_required: bool = True,
    seen: frozenset[str] = frozenset(),
    depth: int = 0,
) -> tuple[set[str], set[str]]:
    _consume()
    if depth >= MAX_STRUCTURE_DEPTH:
        raise SpecReadError("work_limit")
    value = _mapping(schema, label="schema")
    reference = value.get("$ref")
    if isinstance(reference, str):
        if reference in seen:
            return set(), set()
        seen = seen | {reference}
        value = _resolve(root, value)
    required_names = {str(item) for item in value.get("required", [])}
    properties = _mapping(value.get("properties", {}), label="schema properties")
    required: set[str] = set()
    optional: set[str] = set()
    for name, child in properties.items():
        qualified = _schema_path(prefix, name, separator="." if prefix else "")
        field_required = ancestor_required and name in required_names
        (required if field_required else optional).add(qualified)
        child_required, child_optional = _schema_fields(
            root,
            child,
            prefix=qualified,
            ancestor_required=field_required,
            seen=seen,
            depth=depth + 1,
        )
        required.update(child_required)
        optional.update(child_optional)
    items = value.get("items")
    if items is not None:
        child_required, child_optional = _schema_fields(
            root,
            items,
            prefix=_schema_path(prefix, "[]"),
            ancestor_required=ancestor_required,
            seen=seen,
            depth=depth + 1,
        )
        required.update(child_required)
        optional.update(child_optional)
    all_of = value.get("allOf", [])
    if isinstance(all_of, list):
        for branch in all_of:
            child_required, child_optional = _schema_fields(
                root,
                branch,
                prefix=prefix,
                ancestor_required=ancestor_required,
                seen=seen,
                depth=depth + 1,
            )
            required.update(child_required)
            optional.update(child_optional)
    for branch_name in ("oneOf", "anyOf"):
        branches = value.get(branch_name, [])
        if not isinstance(branches, list) or not branches:
            continue
        alternatives = [
            _schema_fields(
                root,
                branch,
                prefix=prefix,
                ancestor_required=ancestor_required,
                seen=seen,
                depth=depth + 1,
            )
            for branch in branches
        ]
        alternative_required = set.intersection(
            *(branch_required for branch_required, _ in alternatives)
        )
        alternative_fields: set[str] = set()
        for branch_required, branch_optional in alternatives:
            alternative_fields.update(branch_required)
            alternative_fields.update(branch_optional)
        required.update(alternative_required)
        optional.update(alternative_fields - alternative_required)
    optional.difference_update(required)
    return required, optional


def _request_fields(
    root: Mapping[str, Any], operation: Mapping[str, Any]
) -> tuple[set[str], set[str]]:
    body = operation.get("requestBody")
    if body is None:
        return set(), set()
    body_mapping = _resolve(root, body)
    content = _mapping(body_mapping.get("content", {}), label="request content")
    required: set[str] = set()
    optional: set[str] = set()
    if body_mapping.get("required") is True:
        required.add("body")
    else:
        optional.add("body")
    for media in content.values():
        media_mapping = _mapping(media, label="request media type")
        schema = media_mapping.get("schema")
        if schema is not None:
            found_required, found_optional = _schema_fields(
                root,
                schema,
                prefix="body",
                ancestor_required=body_mapping.get("required") is True,
            )
            required.update(found_required)
            optional.update(found_optional)
    return required, optional


def _response_fields(
    root: Mapping[str, Any], operation: Mapping[str, Any]
) -> tuple[set[str], set[str]]:
    responses = _mapping(operation.get("responses", {}), label="responses")
    required: set[str] = set()
    optional: set[str] = set()
    for status, response in responses.items():
        if not status.startswith("2"):
            continue
        response_mapping = _resolve(root, response)
        content = _mapping(response_mapping.get("content", {}), label="response content")
        for media in content.values():
            media_mapping = _mapping(media, label="response media type")
            schema = media_mapping.get("schema")
            if schema is not None:
                found_required, found_optional = _schema_fields(root, schema)
                required.update(found_required)
                optional.update(found_optional)
    return required, optional


def _operations(root: Mapping[str, Any]) -> dict[str, Operation]:
    paths = _mapping(root["paths"], label="OpenAPI paths")
    result: dict[str, Operation] = {}
    for path, path_item_value in paths.items():
        path_item = _mapping(path_item_value, label=f"path {path}")
        inherited = path_item.get("parameters", [])
        for method, operation_value in path_item.items():
            if method.lower() not in HTTP_METHODS:
                continue
            operation = _mapping(operation_value, label=f"operation {method} {path}")
            operation_id = operation.get("operationId")
            if not isinstance(operation_id, str) or not operation_id:
                operation_id = f"{method.lower()} {path}"
            if operation_id in result:
                raise SpecReadError(f"Duplicate operationId: {operation_id}")
            parameters: list[object] = []
            for group in (inherited, operation.get("parameters", [])):
                if not isinstance(group, list):
                    raise SpecReadError("Operation parameters must be a list")
                parameters.extend(group)
            required_inputs = {
                _parameter_path(parameter)
                for item in parameters
                for parameter in [_resolve(root, item)]
                if parameter.get("required") is True
            }
            optional_inputs = {
                _parameter_path(parameter)
                for item in parameters
                for parameter in [_resolve(root, item)]
                if parameter.get("required") is not True
            }
            required_body, optional_body = _request_fields(root, operation)
            required_inputs.update(required_body)
            optional_inputs.update(optional_body)
            required_response, optional_response = _response_fields(root, operation)
            result[operation_id] = Operation(
                operation_id,
                method.lower(),
                path,
                frozenset(required_inputs),
                frozenset(optional_inputs),
                frozenset(required_response),
                frozenset(optional_response),
            )
    return result


def _read_only(operation: Operation) -> bool:
    prefix = operation.operation_id.lower()
    return operation.method in {"get", "head", "options"} or prefix.startswith(
        ("get", "list", "query", "search", "read")
    )


def _change(
    severity: Literal["informational", "high"], kind: str, operation: str, detail: str
) -> DriftChange:
    return DriftChange(severity, kind, operation, detail)


def compare_openapi(pinned: bytes, current: bytes, *, stop: Event | None = None) -> DriftReport:
    token = _WORK_BUDGET.set(_WorkBudget(stop))
    try:
        return _compare_openapi(pinned, current)
    finally:
        _WORK_BUDGET.reset(token)


def _compare_openapi(pinned: bytes, current: bytes) -> DriftReport:
    pinned_operations = _operations(_load(pinned))
    current_operations = _operations(_load(current))
    changes: list[DriftChange] = []
    for operation_id in sorted(pinned_operations.keys() | current_operations.keys()):
        before = pinned_operations.get(operation_id)
        after = current_operations.get(operation_id)
        if before is None:
            assert after is not None
            read_only = _read_only(after)
            changes.append(
                _change(
                    "informational" if read_only else "high",
                    "read_operation_added" if read_only else "operation_added",
                    operation_id,
                    f"{after.method.upper()} {after.path}",
                )
            )
            continue
        if after is None:
            changes.append(
                _change(
                    "high",
                    "operation_removed",
                    operation_id,
                    f"{before.method.upper()} {before.path}",
                )
            )
            continue
        if before.path != after.path or before.method != after.method:
            changes.append(
                _change(
                    "high",
                    "operation_path_changed",
                    operation_id,
                    f"{before.method.upper()} {before.path} -> {after.method.upper()} {after.path}",
                )
            )
        for field in sorted(after.required_inputs - before.required_inputs):
            changes.append(_change("high", "required_input_added", operation_id, field))
        for field in sorted(before.required_inputs - after.required_inputs):
            changes.append(
                _change("high", "required_input_removed_or_relaxed", operation_id, field)
            )
        new_optional_inputs = (
            after.optional_inputs - before.optional_inputs - before.required_inputs
        )
        for field in sorted(new_optional_inputs):
            changes.append(_change("informational", "optional_input_added", operation_id, field))
        for field in sorted(after.required_response_fields - before.required_response_fields):
            changes.append(
                _change("high", "required_response_field_added", operation_id, field)
            )
        for field in sorted(before.required_response_fields - after.required_response_fields):
            changes.append(
                _change(
                    "high",
                    "required_response_field_removed_or_relaxed",
                    operation_id,
                    field,
                )
            )
        new_optional = (
            after.optional_response_fields
            - before.optional_response_fields
            - before.required_response_fields
        )
        for field in sorted(new_optional):
            changes.append(
                _change("informational", "optional_response_field_added", operation_id, field)
            )
    severity: Severity = "none"
    if any(change.severity == "high" for change in changes):
        severity = "high"
    elif changes:
        severity = "informational"
    return DriftReport(
        severity,
        hashlib.sha256(pinned).hexdigest(),
        hashlib.sha256(current).hexdigest(),
        tuple(changes),
    )


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "maimemo-mcp-drift-check/1"})
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
        document = bytes(response.read(MAX_DOCUMENT_BYTES + 1))
    if len(document) > MAX_DOCUMENT_BYTES:
        raise SpecReadError("size_limit")
    return document


def read_pinned(path: Path) -> bytes:
    with path.open("rb") as handle:
        document = handle.read(MAX_DOCUMENT_BYTES + 1)
    if len(document) > MAX_DOCUMENT_BYTES:
        raise SpecReadError("size_limit")
    return document


async def fetch_openapi(client: httpx.AsyncClient, url: str) -> bytes:
    document = bytearray()
    async with client.stream(
        "GET", url, headers={"User-Agent": "maimemo-mcp-drift-check/1"}, timeout=20,
    ) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            if len(document) + len(chunk) > MAX_DOCUMENT_BYTES:
                raise SpecReadError("size_limit")
            document.extend(chunk)
    return bytes(document)


def safe_error_category(error: Exception) -> str:
    """Return fixed categories without serializing exception text or custom class names."""
    if isinstance(error, SpecReadError):
        return "SpecReadError"
    if isinstance(error, httpx.HTTPError):
        return "HTTPError"
    if isinstance(error, OSError):
        return "OSError"
    return "UnexpectedError"


def _write_state(path: Path, report: DriftReport, checked_at: datetime) -> None:
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise ValueError("Drift check timestamp must be timezone-aware")
    payload = {
        "severity": report.severity,
        "checked_at": checked_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "pinned_sha256": report.pinned_sha256,
        "current_sha256": report.current_sha256,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Contains only public hashes, time and severity. Every replacement inode
        # must remain readable by the non-root service UID, independent of writer UID.
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pinned", required=True)
    parser.add_argument("--remote", required=True)
    parser.add_argument(
        "--state-file",
        default=os.environ.get("MAIMEMO_OPENAPI_DRIFT_STATE_FILE", "var/openapi-drift.json"),
    )
    arguments = parser.parse_args(argv)
    try:
        pinned = read_pinned(Path(arguments.pinned))
        current = _download(arguments.remote)
        report = compare_openapi(pinned, current)
        _write_state(Path(arguments.state_file), report, datetime.now(UTC))
    except Exception as exc:
        print(
            json.dumps(
                {"severity": "unreadable", "error_class": safe_error_category(exc)},
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(report.as_dict(), ensure_ascii=True, sort_keys=True))
    return 1 if report.severity == "high" else 0


if __name__ == "__main__":
    sys.exit(main())
