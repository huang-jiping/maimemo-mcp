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
class SchemaBranch:
    """One reachable conjunction; alternatives remain separate branches."""

    constraints: tuple[dict[str, Any], ...]


def resolve_reference(reference: object, root: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(reference, str) or not reference.startswith("#/"):
        raise ValueError("only local schema references are supported")
    resolved: Any = root
    for token in reference[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(resolved, dict) or token not in resolved:
            raise ValueError("schema reference does not resolve")
        resolved = resolved[token]
    if not isinstance(resolved, dict):
        raise ValueError("schema reference target is not an object")
    return resolved


def combine(
    left: list[SchemaBranch], right: list[SchemaBranch]
) -> list[SchemaBranch]:
    return [
        SchemaBranch(first.constraints + second.constraints)
        for first in left
        for second in right
    ]


def expand_schema(
    schema: dict[str, Any],
    root: dict[str, Any],
    references: tuple[str, ...] = (),
) -> list[SchemaBranch]:
    unsupported = {"allOf", "oneOf", "not", "if", "then", "else"} & schema.keys()
    if unsupported:
        raise ValueError(f"unsupported schema keyword: {sorted(unsupported)[0]}")
    if "$ref" in schema:
        reference = schema["$ref"]
        if not isinstance(reference, str):
            raise ValueError("schema reference must be a string")
        if reference in references:
            raise ValueError("cyclic schema reference")
        referred = expand_schema(
            resolve_reference(reference, root), root, references + (reference,)
        )
        siblings = {key: value for key, value in schema.items() if key != "$ref"}
        # Draft 2020-12 applies $ref and its siblings as a conjunction.
        if not siblings:
            return referred
        return combine(referred, expand_schema(siblings, root, references))
    if "anyOf" in schema:
        alternatives = schema["anyOf"]
        if not isinstance(alternatives, list) or not alternatives:
            raise ValueError("schema anyOf must be a non-empty list")
        siblings = {key: value for key, value in schema.items() if key != "anyOf"}
        shared = expand_schema(siblings, root, references) if siblings else [SchemaBranch(())]
        expanded: list[SchemaBranch] = []
        for alternative in alternatives:
            if not isinstance(alternative, dict):
                raise ValueError("schema anyOf alternative must be an object")
            expanded.extend(combine(shared, expand_schema(alternative, root, references)))
        return expanded
    return [SchemaBranch((schema,))]


def branch_types(branch: SchemaBranch, *, label: str) -> frozenset[str]:
    possible: set[str] | None = None
    for constraint in branch.constraints:
        declared = constraint.get("type")
        if declared is None:
            if "properties" in constraint:
                declared = "object"
            elif "items" in constraint:
                declared = "array"
            else:
                continue
        values = {declared} if isinstance(declared, str) else set(declared)
        if not values or not all(isinstance(value, str) for value in values):
            raise ValueError(f"{label} schema type is invalid")
        possible = values if possible is None else possible & values
    if possible is None:
        raise ValueError(f"{label} schema has no explicit type")
    return frozenset(possible)


def branch_format(branch: SchemaBranch, *, label: str) -> str | None:
    formats = {
        constraint["format"]
        for constraint in branch.constraints
        if isinstance(constraint.get("format"), str)
    }
    if len(formats) > 1:
        raise ValueError(f"{label} schema has incompatible format constraints")
    return next(iter(formats), None)


def path_branches(
    root: dict[str, Any], path: str, *, label: str, source: bool
) -> list[SchemaBranch]:
    if not path or any(not token for token in path.split(".")):
        raise ValueError(f"{label} path is invalid")
    current = expand_schema(root, root)
    for token in path.split("."):
        following: list[SchemaBranch] = []
        for branch in current:
            types = branch_types(branch, label=label)
            if not types:
                continue  # Unsatisfiable conjunction is unreachable.
            if "null" in types:
                raise ValueError(f"{label} path has a reachable null branch")
            if types == {"object"}:
                children: list[dict[str, Any]] = []
                required = False
                for constraint in branch.constraints:
                    properties = constraint.get("properties")
                    if not isinstance(properties, dict):
                        continue
                    if token in properties:
                        child = properties[token]
                        if not isinstance(child, dict):
                            raise ValueError(f"{label} property schema is invalid")
                        children.append(child)
                        required |= token in constraint.get("required", [])
                    elif constraint.get("additionalProperties") is False:
                        raise ValueError(f"{label} path does not exist")
                if not children:
                    raise ValueError(f"{label} path does not exist")
                if source and not required:
                    raise ValueError(f"{label} path may be missing")
                child_branches = [SchemaBranch(())]
                for child in children:
                    child_branches = combine(child_branches, expand_schema(child, root))
                following.extend(child_branches)
            elif types == {"array"}:
                if token == "[]" or not token.isdigit():
                    raise ValueError(f"{label} path cannot safely select an array item")
                index = int(token)
                items: list[dict[str, Any]] = []
                guaranteed = 0
                for constraint in branch.constraints:
                    if "prefixItems" in constraint:
                        raise ValueError(f"{label} path uses unsupported prefixItems")
                    item = constraint.get("items")
                    if isinstance(item, dict):
                        items.append(item)
                    minimum = constraint.get("minItems")
                    if isinstance(minimum, int):
                        guaranteed = max(guaranteed, minimum)
                if not items or source and guaranteed <= index:
                    raise ValueError(f"{label} array item may be missing")
                item_branches = [SchemaBranch(())]
                for item in items:
                    item_branches = combine(item_branches, expand_schema(item, root))
                following.extend(item_branches)
            else:
                raise ValueError(f"{label} path crosses incompatible schema types")
        if not following:
            raise ValueError(f"{label} path does not resolve")
        current = following
    terminal = [branch for branch in current if branch_types(branch, label=label)]
    if not terminal:
        raise ValueError(f"{label} path has no reachable value")
    return terminal


def branch_is_accepted(source: SchemaBranch, target: SchemaBranch) -> bool:
    source_types = branch_types(source, label="result")
    target_types = branch_types(target, label="argument")
    accepted_types = target_types | ({"integer"} if "number" in target_types else set())
    if not source_types <= accepted_types:
        return False
    target_format = branch_format(target, label="argument")
    return target_format is None or branch_format(source, label="result") == target_format


def validate_schema_reference(
    source_root: dict[str, Any],
    source_path: str,
    target_root: dict[str, Any],
    target_path: str,
) -> list[SchemaBranch]:
    sources = path_branches(source_root, source_path, label="result", source=True)
    targets = path_branches(target_root, target_path, label="argument", source=False)
    # Universal over every reachable source branch; target anyOf is an accepting union.
    if any(not any(branch_is_accepted(source, target) for target in targets) for source in sources):
        raise ValueError("dynamic reference source and target schemas are incompatible")
    return targets


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
    target_branches = validate_schema_reference(
        source_schema,
        reuse["result_path"],
        target_schema,
        reuse["argument_path"],
    )
    # Current corpus intentionally supports only scalar strings. New shapes must add tests first.
    if any(branch_types(branch, label="argument") != {"string"} for branch in target_branches):
        raise ValueError("dynamic references currently support only string targets")
    formats = {branch_format(branch, label="argument") for branch in target_branches}
    target[parts[-1]] = (
        "00000000-0000-4000-8000-000000000001"
        if "uuid" in formats
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


def test_schema_reference_compatibility_is_branch_safe_and_order_independent() -> None:
    uuid_string = {"type": "string", "format": "uuid"}
    plain_string = {"type": "string"}
    uuid_target = {
        "type": "object",
        "properties": {"value": uuid_string},
        "required": ["value"],
    }
    plain_target = {
        "type": "object",
        "properties": {"value": plain_string},
        "required": ["value"],
    }

    for alternatives in (
        [uuid_string, plain_string],
        [plain_string, uuid_string],
    ):
        source = {
            "type": "object",
            "properties": {"value": {"anyOf": alternatives}},
            "required": ["value"],
        }
        with pytest.raises(ValueError, match="incompatible"):
            validate_schema_reference(source, "value", uuid_target, "value")

    all_uuid = {
        "type": "object",
        "properties": {"value": {"anyOf": [uuid_string, uuid_string]}},
        "required": ["value"],
    }
    validate_schema_reference(all_uuid, "value", uuid_target, "value")
    validate_schema_reference(plain_target, "value", plain_target, "value")
    validate_schema_reference(uuid_target, "value", plain_target, "value")


def test_schema_reference_preserves_ref_siblings_and_rejects_bad_refs() -> None:
    source = {
        "$defs": {"Text": {"type": "string"}},
        "type": "object",
        "properties": {"value": {"$ref": "#/$defs/Text", "format": "uuid"}},
        "required": ["value"],
    }
    target = {
        "type": "object",
        "properties": {"value": {"type": "string", "format": "uuid"}},
        "required": ["value"],
    }
    validate_schema_reference(source, "value", target, "value")

    missing = copy.deepcopy(source)
    missing["properties"]["value"] = {"$ref": "#/$defs/Missing"}
    with pytest.raises(ValueError, match="does not resolve"):
        validate_schema_reference(missing, "value", target, "value")

    cycle = {
        "$defs": {"A": {"$ref": "#/$defs/B"}, "B": {"$ref": "#/$defs/A"}},
        "type": "object",
        "properties": {"value": {"$ref": "#/$defs/A"}},
        "required": ["value"],
    }
    with pytest.raises(ValueError, match="cyclic"):
        validate_schema_reference(cycle, "value", target, "value")


def test_nullable_intermediate_reference_is_rejected_independent_of_order() -> None:
    object_branch = {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    }
    target = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }
    for alternatives in ([object_branch, {"type": "null"}], [{"type": "null"}, object_branch]):
        source = {
            "type": "object",
            "properties": {"data": {"anyOf": alternatives}},
            "required": ["data"],
        }
        with pytest.raises(ValueError, match="null"):
            validate_schema_reference(source, "data.id", target, "value")


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
