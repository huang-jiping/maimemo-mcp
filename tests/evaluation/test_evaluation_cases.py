"""Corpus shape and live MCP schema checks; these do not test model selection."""

import copy
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator
from mcp.client import Client

from maimemo_mcp.config import Settings
from maimemo_mcp.mcp_server.app import create_mcp_app

CORPUS = Path(__file__).with_name("prompts.yaml")
COMPOSITES = {
    "get_daily_study_dashboard": "dashboard",
    "get_word_learning_profile": "word_profile",
    "get_weak_words": "weak_words",
    "get_due_review_overview": "due_review",
    "get_learning_data_health": "data_health",
}
REFERENCE = re.compile(r"^\$\{turn:(\d+):([a-z_.]+)}$")


def load_cases() -> list[dict[str, Any]]:
    document = yaml.safe_load(CORPUS.read_text(encoding="utf-8"))
    assert document["version"] == 1
    assert "model tool choice" in document["notes"]
    return document["cases"]


@pytest.fixture
def evaluation_settings(tmp_path: Path) -> Settings:
    token = tmp_path / "token"
    key = tmp_path / "fingerprint-key"
    token.write_text("evaluation-fake-token", encoding="utf-8")
    key.write_text("evaluation-fake-key", encoding="utf-8")
    return Settings(
        database_url="postgresql+psycopg://evaluation:evaluation@127.0.0.1:1/evaluation",
        token_file=token,
        token_fingerprint_key_file=key,
    )


def test_every_composite_has_direct_and_indirect_coverage() -> None:
    cases = load_cases()
    for tool, intent in COMPOSITES.items():
        matching = [case for case in cases if case["intent"] == intent]
        assert any(case["category"] == "direct" for case in matching), tool
        assert any(
            case["category"] in {"indirect", "follow_up", "partial", "stale"}
            for case in matching
        ), tool
        assert all(turn.get("expected_tool") == tool for case in matching for turn in case["turns"])


def test_required_multiturn_and_data_quality_scenarios_are_present() -> None:
    cases = load_cases()
    categories = {case["category"] for case in cases}
    assert {
        "follow_up",
        "stale",
        "partial",
        "explicit_feedback",
        "speculative_feedback",
        "unsupported_upstream_write",
    } <= categories
    assert any(
        "identifier_reuse" in turn
        for case in cases
        for turn in case["turns"]
    )


def test_data_quality_scenarios_are_specific_and_reproducible() -> None:
    by_category = {case["category"]: case for case in load_cases()}
    expectations = {
        "partial": ("partial", {"analysis_coverage_partial"}, "oldest_scored_evidence_cutoff"),
        "stale": (
            "stale",
            {"today_stale", "records_stale"},
            "oldest_required_task_success",
        ),
    }
    for category, (result_class, warnings, cutoff) in expectations.items():
        turn = by_category[category]["turns"][0]
        scenario = turn["scenario"]
        assert scenario["kind"] == "disposable_postgres"
        assert len(scenario["setup"]) >= 2
        datetime.fromisoformat(scenario["evaluate_at"].replace("Z", "+00:00"))
        expected = turn["expected_result"]
        assert expected["class"] == result_class
        assert set(expected["required_warnings"]) == warnings
        assert expected["data_through"] == "2030-10-0" + ("2" if category == "partial" else "1")
        assert expected["cutoff_behavior"] == cutoff


@dataclass(frozen=True)
class SchemaShape:
    types: frozenset[str]
    formats: frozenset[str]
    nullable: bool
    may_be_missing: bool


def dereference(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    seen: set[str] = set()
    while "$ref" in schema:
        reference = schema["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/") or reference in seen:
            raise ValueError("only acyclic local schema references are supported")
        seen.add(reference)
        resolved: Any = root
        for token in reference[2:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            if not isinstance(resolved, dict) or token not in resolved:
                raise ValueError("schema reference does not resolve")
            resolved = resolved[token]
        if not isinstance(resolved, dict):
            raise ValueError("schema reference target is not an object")
        schema = resolved
    return schema


def variants(schema: dict[str, Any], root: dict[str, Any]) -> list[dict[str, Any]]:
    schema = dereference(schema, root)
    alternatives = schema.get("anyOf")
    if alternatives is None:
        return [schema]
    if not isinstance(alternatives, list) or not alternatives:
        raise ValueError("schema anyOf must be a non-empty list")
    expanded: list[dict[str, Any]] = []
    for alternative in alternatives:
        if not isinstance(alternative, dict):
            raise ValueError("schema anyOf alternative must be an object")
        expanded.extend(variants(alternative, root))
    return expanded


def schema_path(root: dict[str, Any], path: str, *, label: str) -> SchemaShape:
    if not path or any(not token for token in path.split(".")):
        raise ValueError(f"{label} path is invalid")
    current: list[tuple[dict[str, Any], bool, bool]] = [(root, False, False)]
    for token in path.split("."):
        following: list[tuple[dict[str, Any], bool, bool]] = []
        for schema, nullable, missing in current:
            non_null = False
            for option in variants(schema, root):
                option_type = option.get("type")
                if option_type == "null":
                    nullable = True
                    continue
                non_null = True
                properties = option.get("properties")
                if option_type == "object" or isinstance(properties, dict):
                    if not isinstance(properties, dict) or token not in properties:
                        raise ValueError(f"{label} path does not exist")
                    required = option.get("required", [])
                    following.append(
                        (properties[token], nullable, missing or token not in required)
                    )
                elif option_type == "array":
                    if token != "[]" and not token.isdigit():
                        raise ValueError(f"{label} path must select an array item")
                    items = option.get("items")
                    if not isinstance(items, dict):
                        raise ValueError(f"{label} array has no supported item schema")
                    following.append((items, nullable, missing))
                else:
                    raise ValueError(f"{label} path crosses a scalar")
            if not non_null:
                raise ValueError(f"{label} path resolves only to null")
        if not following:
            raise ValueError(f"{label} path does not resolve")
        current = following

    types: set[str] = set()
    formats: set[str] = set()
    nullable = False
    may_be_missing = False
    for schema, inherited_nullable, inherited_missing in current:
        nullable |= inherited_nullable
        may_be_missing |= inherited_missing
        for option in variants(schema, root):
            option_type = option.get("type")
            if option_type == "null":
                nullable = True
                continue
            if not isinstance(option_type, str):
                raise ValueError(f"{label} terminal schema has no explicit type")
            types.add(option_type)
            if isinstance(option.get("format"), str):
                formats.add(option["format"])
    if not types:
        raise ValueError(f"{label} path has no value type")
    return SchemaShape(frozenset(types), frozenset(formats), nullable, may_be_missing)


def assert_compatible(source: SchemaShape, target: SchemaShape) -> None:
    if source.may_be_missing or source.nullable and not target.nullable:
        raise ValueError("dynamic reference source is incompatible with required target")
    compatible = target.types | ({"integer"} if "number" in target.types else set())
    if not source.types <= compatible:
        raise ValueError("dynamic reference source and target types are incompatible")
    if target.formats and not target.formats <= source.formats:
        raise ValueError("dynamic reference source and target formats are incompatible")


def resolved_arguments(
    case: dict[str, Any],
    turn: dict[str, Any],
    turn_number: int,
    registered: dict[str, Any],
) -> dict[str, Any]:
    arguments = copy.deepcopy(turn["expected_arguments"])
    reuse = turn.get("identifier_reuse")
    if reuse is None:
        return arguments
    assert set(reuse) == {"from_turn", "result_path", "argument_path"}
    assert 1 <= reuse["from_turn"] < turn_number
    assert reuse["result_path"].startswith("data.")
    target: Any = arguments
    parts = reuse["argument_path"].split(".")
    for part in parts[:-1]:
        target = target[part]
    placeholder = target[parts[-1]]
    match = REFERENCE.fullmatch(placeholder)
    assert match is not None
    assert int(match.group(1)) == reuse["from_turn"]
    assert match.group(2) == reuse["result_path"]
    source_turn = case["turns"][reuse["from_turn"] - 1]
    source_tool_name = source_turn.get("expected_tool")
    target_tool_name = turn.get("expected_tool")
    if source_tool_name not in registered or target_tool_name not in registered:
        raise ValueError("dynamic reference tool is not registered")
    source_schema = registered[source_tool_name].output_schema
    target_schema = registered[target_tool_name].input_schema
    if not isinstance(source_schema, dict) or not isinstance(target_schema, dict):
        raise ValueError("dynamic reference tool schema is unavailable")
    source_shape = schema_path(source_schema, reuse["result_path"], label="result")
    target_shape = schema_path(target_schema, reuse["argument_path"], label="argument")
    # Current corpus intentionally supports only scalar strings. New shapes must add tests first.
    if target_shape.types != frozenset({"string"}):
        raise ValueError("dynamic references currently support only string targets")
    assert_compatible(source_shape, target_shape)
    target[parts[-1]] = (
        "00000000-0000-4000-8000-000000000001"
        if "uuid" in target_shape.formats
        else "resolved-spelling"
    )
    return arguments


def test_identifier_reuse_is_same_case_backward_and_dynamic() -> None:
    reuse_turns = []
    for case in load_cases():
        for number, turn in enumerate(case["turns"], start=1):
            if "identifier_reuse" in turn:
                reuse_turns.append((case["id"], number, turn))
    assert {(case_id, turn["expected_tool"]) for case_id, _, turn in reuse_turns} == {
        ("profile-indirect-followup", "get_word_learning_profile"),
        ("feedback-explicit", "retract_feedback"),
    }


async def test_identifier_reuse_paths_and_types_match_live_tool_schemas(
    evaluation_settings: Settings,
) -> None:
    async with Client(create_mcp_app(evaluation_settings).sdk) as client:
        registered = {tool.name: tool for tool in (await client.list_tools()).tools}
    documents = load_cases()
    for case in documents:
        for turn_number, turn in enumerate(case["turns"], start=1):
            resolved_arguments(case, turn, turn_number, registered)

    profile = next(case for case in documents if case["id"] == "profile-indirect-followup")
    missing = copy.deepcopy(profile)
    missing["turns"][1]["identifier_reuse"]["result_path"] = "data.does_not_exist"
    missing["turns"][1]["expected_arguments"]["request"]["spelling"] = (
        "${turn:1:data.does_not_exist}"
    )
    with pytest.raises(ValueError, match="result path"):
        resolved_arguments(missing, missing["turns"][1], 2, registered)

    mismatch = copy.deepcopy(profile)
    mismatch["turns"][1]["identifier_reuse"]["result_path"] = "data.profiles"
    mismatch["turns"][1]["expected_arguments"]["request"]["spelling"] = (
        "${turn:1:data.profiles}"
    )
    with pytest.raises(ValueError, match="incompatible"):
        resolved_arguments(mismatch, mismatch["turns"][1], 2, registered)

    forward = copy.deepcopy(profile)
    forward["turns"][1]["identifier_reuse"]["from_turn"] = 2
    forward["turns"][1]["expected_arguments"]["request"]["spelling"] = (
        "${turn:2:data.spelling}"
    )
    with pytest.raises(AssertionError):
        resolved_arguments(forward, forward["turns"][1], 2, registered)

    cross_case = copy.deepcopy(profile)
    cross_case["turns"][1]["identifier_reuse"] = {
        "from_case": "feedback-explicit",
        "from_turn": 1,
        "result_path": "data.id",
        "argument_path": "request.spelling",
    }
    with pytest.raises(AssertionError):
        resolved_arguments(cross_case, cross_case["turns"][1], 2, registered)


async def test_expected_tools_exist_and_arguments_match_live_schemas(
    evaluation_settings: Settings,
) -> None:
    async with Client(create_mcp_app(evaluation_settings).sdk) as client:
        registered = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert len(registered) == 24
    for case in load_cases():
        for turn_number, turn in enumerate(case["turns"], start=1):
            tool_name = turn.get("expected_tool")
            if tool_name is None:
                continue
            assert tool_name in registered, case["id"]
            errors = sorted(
                Draft202012Validator(registered[tool_name].input_schema).iter_errors(
                    resolved_arguments(case, turn, turn_number, registered)
                ),
                key=lambda error: list(error.path),
            )
            assert not errors, f"{case['id']}: {[error.message for error in errors]}"
