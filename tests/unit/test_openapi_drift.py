"""Semantic OpenAPI drift classification, independent of YAML presentation."""

import hashlib
import json
from pathlib import Path
from textwrap import dedent, indent
from time import perf_counter

import pytest
import yaml
from maimemo import openapi_drift as drift

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


def test_removed_or_relaxed_required_inputs_and_responses_are_high() -> None:
    pinned = spec(
        """
          /api/v1/words:
            post:
              operationId: queryWords
              parameters:
                - {name: limit, in: query, required: true, schema: {type: integer}}
              requestBody:
                required: true
                content:
                  application/json:
                    schema:
                      type: object
                      required: [query]
                      properties: {query: {type: string}}
              responses:
                '200':
                  description: ok
                  content:
                    application/json:
                      schema:
                        type: object
                        required: [items]
                        properties: {items: {type: array}}
        """
    )
    relaxed = pinned.replace(b"required: true", b"required: false", 1)
    relaxed = relaxed.replace(b"required: [query]", b"required: []")
    relaxed = relaxed.replace(b"required: [items]", b"required: []")
    report = compare_openapi(pinned, relaxed)
    assert report.severity == "high"
    assert {change.kind for change in report.changes} >= {
        "required_input_removed_or_relaxed",
        "required_response_field_removed_or_relaxed",
    }
    assert {change.detail for change in report.changes} >= {
        "query:limit",
        "body.query",
        "items",
    }


def test_optional_parent_does_not_make_required_descendants_globally_required() -> None:
    current = BASE.replace(
        b"cursor: {type: string}",
        b"""cursor: {type: string}
        metadata:
          type: object
          required: [id]
          properties: {id: {type: string}}
        annotations:
          type: array
          items:
            type: object
            required: [id]
            properties: {id: {type: string}}""",
    )
    report = compare_openapi(BASE, current)
    assert report.severity == "informational"
    assert not any(change.kind == "required_response_field_added" for change in report.changes)
    details = {change.detail for change in report.changes}
    assert {"metadata", "metadata.id", "annotations", "annotations[].id"} <= details


def test_all_of_requirements_are_global() -> None:
    all_of = BASE.replace(
        b"schema: {$ref: '#/components/schemas/Words'}",
        b"""schema:
                    allOf:
                      - {$ref: '#/components/schemas/Words'}
                      - type: object
                        required: [revision]
                        properties: {revision: {type: integer}}""",
    )
    report = compare_openapi(BASE, all_of)
    assert report.severity == "high"
    assert any(
        change.kind == "required_response_field_added" and change.detail == "revision"
        for change in report.changes
    )

@pytest.mark.parametrize("branch_keyword", ["oneOf", "anyOf"])
def test_alternative_branch_requirements_are_not_global(branch_keyword: str) -> None:
    alternatives = spec(
        f"""
          /api/v1/words:
            get:
              operationId: getWords
              responses:
                '200':
                  description: ok
                  content:
                    application/json:
                      schema:
                        {branch_keyword}:
                          - type: object
                            required: [kind]
                            properties: {{kind: {{type: string}}}}
                          - type: object
                            required: [kind]
                            properties: {{kind: {{type: string}}}}
        """
    )
    alternative_branch_tightened = spec(
        f"""
          /api/v1/words:
            get:
              operationId: getWords
              responses:
                '200':
                  description: ok
                  content:
                    application/json:
                      schema:
                        {branch_keyword}:
                          - type: object
                            required: [kind, alternate]
                            properties: {{kind: {{type: string}}, alternate: {{type: string}}}}
                          - type: object
                            required: [kind]
                            properties: {{kind: {{type: string}}}}
        """
    )
    report = compare_openapi(alternatives, alternative_branch_tightened)
    assert report.severity == "informational"
    assert not any(change.kind == "required_response_field_added" for change in report.changes)


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
    state = tmp_path / "drift-state.json"
    pinned.write_bytes(BASE)
    monkeypatch.setattr(drift, "_download", lambda url: current)
    assert (
        drift.main(
            [
                "--pinned",
                str(pinned),
                "--remote",
                "https://example.test/spec",
                "--state-file",
                str(state),
            ]
        )
        == expected
    )


def test_cli_atomically_persists_safe_latest_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned = tmp_path / "pinned.yaml"
    state = tmp_path / "drift-state.json"
    pinned.write_bytes(BASE)
    monkeypatch.setattr(drift, "_download", lambda url: BASE)
    assert (
        drift.main(
            [
                "--pinned",
                str(pinned),
                "--remote",
                "https://example.test/spec",
                "--state-file",
                str(state),
            ]
        )
        == 0
    )
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["severity"] == "none"
    assert saved["pinned_sha256"] == saved["current_sha256"]
    assert saved["checked_at"].endswith("Z")
    assert set(saved) == {"severity", "checked_at", "pinned_sha256", "current_sha256"}
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "drift-state.json",
        "pinned.yaml",
    ]


def test_hashes_are_of_exact_document_bytes() -> None:
    report = compare_openapi(BASE, BASE + b"\n")
    assert report.pinned_sha256 == hashlib.sha256(BASE).hexdigest()
    assert report.current_sha256 == hashlib.sha256(BASE + b"\n").hexdigest()
    assert report.severity == "none"


@pytest.mark.parametrize("side", ["pinned", "current"])
def test_oversized_document_is_rejected_before_parsing(side: str) -> None:
    oversized = b" " * (drift.MAX_DOCUMENT_BYTES + 1)
    with pytest.raises(drift.SpecReadError, match="size_limit"):
        compare_openapi(oversized if side == "pinned" else BASE,
                        oversized if side == "current" else BASE)


def test_failed_atomic_replace_keeps_previous_state_and_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    state = tmp_path / "state.json"
    state.write_text("previous", encoding="utf-8")

    def denied(*args: object) -> None:
        raise PermissionError("SYNTHETIC_PRIVATE_PATH")

    monkeypatch.setattr(drift.os, "replace", denied)
    with pytest.raises(PermissionError):
        drift._write_state(state, compare_openapi(BASE, BASE), datetime.now(UTC))
    assert state.read_text(encoding="utf-8") == "previous"
    assert list(tmp_path.iterdir()) == [state]


def test_cli_failure_does_not_print_exception_text_or_custom_class_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    class SYNTHETIC_SECRET_EXCEPTION(Exception):
        pass

    pinned = tmp_path / "pinned.yaml"
    pinned.write_bytes(BASE)

    def failed(url: str) -> bytes:
        raise SYNTHETIC_SECRET_EXCEPTION("https://u:SYNTHETIC_PASSWORD@example.test body")

    monkeypatch.setattr(drift, "_download", failed)
    assert drift.main(["--pinned", str(pinned), "--remote", "https://example.test"]) == 2
    assert json.loads(capsys.readouterr().out) == {
        "severity": "unreadable", "error_class": "UnexpectedError",
    }


@pytest.mark.parametrize("links", ["aliases", "references", "merges"])
def test_small_exponential_schema_graph_is_rejected_with_fixed_safe_error(links: str) -> None:
    if links == "aliases":
        levels = ["  A0: &a0 {type: object}"]
        levels += [f"  A{i}: &a{i} {{allOf: [*a{i-1}, *a{i-1}]}}" for i in range(1, 19)]
        schema = "*a18"
    elif links == "merges":
        levels = ["  A0: &a0 {type: object}"]
        levels += [f"  A{i}: &a{i} {{<<: [*a{i-1}, *a{i-1}]}}" for i in range(1, 19)]
        schema = "*a18"
    else:
        levels = ["  A0: {type: object}"]
        levels += [
            f"  A{i}: {{allOf: [{{$ref: '#/x/A{i-1}'}}, {{$ref: '#/x/A{i-1}'}}]}}"
            for i in range(1, 19)
        ]
        schema = "{$ref: '#/x/A18'}"
    document = (
        "openapi: 3.0.0\nx:\n" + "\n".join(levels) + "\npaths:\n"
        "  /test:\n    get:\n      operationId: getTest\n      responses:\n"
        "        '200':\n          content:\n            application/json:\n"
        f"              schema: {schema}\n"
    ).encode()
    assert len(document) < 2048
    started = perf_counter()
    with pytest.raises(drift.SpecReadError, match="^(structure_limit|work_limit)$") as failure:
        compare_openapi(BASE, document)
    assert drift.safe_error_category(failure.value) == "SpecReadError"
    assert perf_counter() - started < 0.5


@pytest.mark.parametrize("extension", ["[" * 80 + "0" + "]" * 80, "&a [*a]"])
def test_deep_or_recursive_yaml_is_rejected_before_schema_traversal(extension: str) -> None:
    document = f"openapi: 3.0.0\npaths: {{}}\nx: {extension}\n".encode()
    with pytest.raises(drift.SpecReadError, match="^structure_limit$"):
        compare_openapi(BASE, document)


@pytest.mark.parametrize(
    ("parent_length", "leaves", "category"),
    [
        pytest.param(4096, 256, "path_limit", id="single_reduced"),
        pytest.param(300000, 2000, "path_limit", id="original_size"),
        pytest.param(950, 1200, "path_bytes_limit", id="aggregate"),
        pytest.param(600, 1, "path_limit", id="utf8_single"),
    ],
)
def test_real_pinned_long_parent_fanout_is_bounded_before_paths_are_saved(
    parent_length: int, leaves: int, category: str,
) -> None:
    pinned = drift.DEFAULT_PINNED_FILE.read_bytes()
    parent = "SYNTHETIC_PRIVATE_KEY_" + ("界" if leaves == 1 else "x") * parent_length
    document: dict[str, object] = {"openapi": "3.0.0", "paths": {}}
    document["paths"]["/fanout-budget-probe"] = {
        "get": {
            "operationId": "getFanoutBudgetProbe",
            "responses": {"200": {"description": "ok", "content": {
                "application/json": {"schema": {"properties": {
                    parent: {"properties": {
                        f"leaf{i}": {"type": "string"} for i in range(leaves)
                    }},
                }}},
            }}},
        },
    }
    current = yaml.safe_dump(document, allow_unicode=True).encode("utf-8")
    assert len(current) < 2 * 1024 * 1024
    with pytest.raises(drift.SpecReadError, match=f"^{category}$") as failure:
        compare_openapi(pinned, current)
    assert parent not in str(failure.value)
    assert drift.safe_error_category(failure.value) == "SpecReadError"


def test_real_pinned_result_is_unchanged_under_path_budgets() -> None:
    pinned = drift.DEFAULT_PINNED_FILE.read_bytes()
    report = compare_openapi(pinned, pinned)
    assert report.severity == "none"
    assert report.changes == ()
    assert report.pinned_sha256 == report.current_sha256 == hashlib.sha256(pinned).hexdigest()


@pytest.mark.parametrize("field", ["name", "in"])
@pytest.mark.parametrize("shape", ["alias_list", "deep_mapping", "integer", "null"])
def test_parameter_container_or_nonstring_is_rejected_before_rendering(
    field: str, shape: str,
) -> None:
    scalar = "SYNTHETIC_PRIVATE_PARAMETER_" + "x" * 4096
    if shape == "alias_list":
        # Container aliases are emitted by SafeDumper; scalar strings alone are not.
        value: object = [[scalar]] * 64
    elif shape == "deep_mapping":
        value = scalar
        for _ in range(12):
            value = {"nested": value}
    elif shape == "integer":
        value = 42
    else:
        value = None
    parameter = {"name": "limit", "in": "query", "required": False}
    parameter[field] = value
    current = yaml.safe_dump({
        "openapi": "3.0.0", "paths": {"/test": {"get": {
            "operationId": "getTest", "parameters": [parameter],
            "responses": {"200": {"description": "ok"}},
        }}},
    }).encode("utf-8")
    with pytest.raises(drift.SpecReadError, match="^parameter_type$") as failure:
        compare_openapi(drift.DEFAULT_PINNED_FILE.read_bytes(), current)
    assert scalar not in str(failure.value)
    assert drift.safe_error_category(failure.value) == "SpecReadError"


@pytest.mark.parametrize("field", ["name", "in"])
def test_parameter_type_check_never_calls_container_str_or_repr(field: str) -> None:
    rendered: list[str] = []

    class ForbiddenRendering(list[object]):
        def __str__(self) -> str:
            rendered.append("str")
            raise AssertionError("parameter conversion must not happen")

        def __repr__(self) -> str:
            rendered.append("repr")
            raise AssertionError("parameter conversion must not happen")

    parameter: dict[str, object] = {"name": "limit", "in": "query"}
    parameter[field] = ForbiddenRendering()
    with pytest.raises(drift.SpecReadError, match="^parameter_type$"):
        drift._parameter_path(parameter)
    assert rendered == []


@pytest.mark.parametrize("required", ["field", {"field": "nested"}, [["field"]], [42], None])
def test_required_must_be_a_list_of_strings_before_schema_rendering(required: object) -> None:
    current = yaml.safe_dump({
        "openapi": "3.0.0", "paths": {"/test": {"get": {
            "operationId": "getTest", "responses": {"200": {"content": {
                "application/json": {"schema": {"required": required, "properties": {
                    "field": {"type": "string"},
                }}},
            }}},
        }}},
    }).encode("utf-8")
    with pytest.raises(drift.SpecReadError, match="^required_type$"):
        compare_openapi(drift.DEFAULT_PINNED_FILE.read_bytes(), current)


def test_required_type_check_never_renders_container_elements() -> None:
    class ForbiddenRendering(list[object]):
        def __str__(self) -> str:
            raise AssertionError("required element must not be rendered")

        def __repr__(self) -> str:
            raise AssertionError("required element must not be rendered")

    with pytest.raises(drift.SpecReadError, match="^required_type$"):
        drift._schema_fields({}, {"required": [ForbiddenRendering()], "properties": {}})


def test_mapping_keys_never_render_nonstring_yaml_scalars_or_objects() -> None:
    class ForbiddenRendering:
        def __str__(self) -> str:
            raise AssertionError("mapping key must not be rendered")

        def __repr__(self) -> str:
            raise AssertionError("mapping key must not be rendered")

    for key in (ForbiddenRendering(), b"SYNTHETIC_PRIVATE_BINARY_KEY"):
        with pytest.raises(drift.SpecReadError, match="^mapping_key_type$"):
            drift._mapping({key: {}}, label="schema properties")
