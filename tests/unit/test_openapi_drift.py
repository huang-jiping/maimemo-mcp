"""Semantic OpenAPI drift classification, independent of YAML presentation."""

import importlib.util
import sys
from pathlib import Path
from textwrap import dedent, indent
from typing import Any

import pytest

_MODULE_SPEC = importlib.util.spec_from_file_location(
    "maimemo_openapi_drift",
    Path(__file__).parents[2] / "scripts" / "check_openapi_drift.py",
)
assert _MODULE_SPEC is not None and _MODULE_SPEC.loader is not None
drift: Any = importlib.util.module_from_spec(_MODULE_SPEC)
sys.modules[_MODULE_SPEC.name] = drift
_MODULE_SPEC.loader.exec_module(drift)
compare_openapi = drift.compare_openapi


def spec(paths: str, schemas: str = "") -> bytes:
    path_yaml = indent(dedent(paths).strip(), "  ")
    schema_yaml = indent(dedent(schemas).strip() or "Empty: {type: object}", "    ")
    return (
        "openapi: 3.0.0\n"
        "info: {title: test, version: '1'}\n"
        f"paths:\n{path_yaml}\n"
        f"components:\n  schemas:\n{schema_yaml}\n"
    ).encode()


BASE = spec(
    """
      /api/v1/words:
        get:
          operationId: getWords
          parameters:
            - {name: limit, in: query, required: false, schema: {type: integer}}
          responses:
            '200':
              description: ok
              content:
                application/json:
                  schema: {$ref: '#/components/schemas/Words'}
    """,
    """
        Words:
          type: object
          required: [items]
          properties:
            items: {type: array, items: {type: string}}
            cursor: {type: string}
    """,
)


def test_yaml_reordering_is_not_drift() -> None:
    reordered = b"""openapi: 3.0.0
components:
  schemas:
    Words:
      properties:
        cursor: {type: string}
        items: {items: {type: string}, type: array}
      required: [items]
      type: object
info: {version: '1', title: test}
paths:
  /api/v1/words:
    get:
      responses:
        '200':
          content:
            application/json:
              schema: {$ref: '#/components/schemas/Words'}
          description: ok
      parameters:
        - schema: {type: integer}
          required: false
          in: query
          name: limit
      operationId: getWords
"""
    report = compare_openapi(BASE, reordered)
    assert report.severity == "none"
    assert report.changes == ()


def test_new_optional_field_and_new_read_operation_are_informational() -> None:
    current = spec(
        """
          /api/v1/words:
            get:
              operationId: getWords
              parameters:
                - {name: limit, in: query, required: false, schema: {type: integer}}
                - {name: offset, in: query, required: false, schema: {type: integer}}
              responses:
                '200':
                  description: ok
                  content:
                    application/json:
                      schema: {$ref: '#/components/schemas/Words'}
          /api/v1/words/recent:
            get:
              operationId: listRecentWords
              responses: {'200': {description: ok}}
        """,
        """
            Words:
              type: object
              required: [items]
              properties:
                cursor: {type: string}
                items: {type: array, items: {type: string}}
                page: {type: integer}
        """,
    )
    report = compare_openapi(BASE, current)
    assert report.severity == "informational"
    assert {change.kind for change in report.changes} == {
        "optional_input_added",
        "optional_response_field_added",
        "read_operation_added",
    }


def test_removed_operation_and_changed_path_are_high() -> None:
    removed = compare_openapi(BASE, spec("{}"))
    assert removed.severity == "high"
    assert any(change.kind == "operation_removed" for change in removed.changes)

    moved = compare_openapi(
        BASE,
        BASE.replace(b"/api/v1/words:", b"/api/v2/words:"),
    )
    assert moved.severity == "high"
    assert any(change.kind == "operation_path_changed" for change in moved.changes)


def test_new_required_input_and_response_fields_are_high() -> None:
    required_input = BASE.replace(b"required: false", b"required: true")
    report = compare_openapi(BASE, required_input)
    assert report.severity == "high"
    assert any(change.kind == "required_input_added" for change in report.changes)

    required_response = BASE.replace(b"required: [items]", b"required: [items, cursor]")
    report = compare_openapi(BASE, required_response)
    assert report.severity == "high"
    assert any(change.kind == "required_response_field_added" for change in report.changes)


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (BASE, 0),
        (BASE.replace(b"/api/v1/words:", b"/api/v2/words:"), 1),
        (b"not an openapi document", 2),
    ],
)
def test_cli_is_nonzero_only_for_high_or_unreadable_specs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    current: bytes,
    expected: int,
) -> None:
    pinned = tmp_path / "pinned.yaml"
    pinned.write_bytes(BASE)
    monkeypatch.setattr(drift, "_download", lambda url: current)
    assert (
        drift.main(["--pinned", str(pinned), "--remote", "https://example.test/spec"])
        == expected
    )
