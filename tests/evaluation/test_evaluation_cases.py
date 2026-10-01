"""Corpus shape and live MCP schema checks; these do not test model selection."""

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
        "retraction",
        "unsupported_upstream_write",
    } <= categories
    assert any(
        "identifier_reuse" in turn
        for case in cases
        for turn in case["turns"]
    )


async def test_expected_tools_exist_and_arguments_match_live_schemas(
    evaluation_settings: Settings,
) -> None:
    async with Client(create_mcp_app(evaluation_settings).sdk) as client:
        registered = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert len(registered) == 24
    for case in load_cases():
        for turn in case["turns"]:
            tool_name = turn.get("expected_tool")
            if tool_name is None:
                continue
            assert tool_name in registered, case["id"]
            errors = sorted(
                Draft202012Validator(registered[tool_name].input_schema).iter_errors(
                    turn["expected_arguments"]
                ),
                key=lambda error: list(error.path),
            )
            assert not errors, f"{case['id']}: {[error.message for error in errors]}"
