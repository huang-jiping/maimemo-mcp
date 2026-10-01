"""Deterministic safety checks for the corpus and explicit smoke allowlist."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import yaml
from mcp.client import Client

from maimemo_mcp.config import Settings
from maimemo_mcp.mcp_server.app import create_mcp_app

ROOT = Path(__file__).parents[2]
CORPUS = Path(__file__).with_name("prompts.yaml")
ATOMIC_READS = {
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


def cases() -> list[dict[str, Any]]:
    return yaml.safe_load(CORPUS.read_text(encoding="utf-8"))["cases"]


def load_smoke_module() -> ModuleType:
    path = ROOT / "scripts" / "smoke_readonly_api.py"
    spec = importlib.util.spec_from_file_location("smoke_readonly_api", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_speculative_feedback_never_designates_a_write_tool() -> None:
    speculative = [case for case in cases() if case["category"] == "speculative_feedback"]
    assert speculative
    for case in speculative:
        for turn in case["turns"]:
            assert turn["expected_tool"] is None
            assert turn["expected_behavior"] == "ask_confirmation"
            assert "record_confusion_feedback" in turn["forbidden_tools"]


def test_unsupported_writes_reference_no_registered_capability() -> None:
    unsupported = [case for case in cases() if case["category"] == "unsupported_upstream_write"]
    assert unsupported
    for case in unsupported:
        for turn in case["turns"]:
            assert turn["expected_tool"] is None
            assert turn["expected_behavior"] == "refuse"
            assert turn["forbidden_capability"] not in ATOMIC_READS


def test_smoke_allowlist_is_exact_and_requires_confirmation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    smoke = load_smoke_module()
    assert smoke.READ_ONLY_OPERATIONS == ATOMIC_READS
    with pytest.raises(SystemExit):
        smoke.parse_args([])
    with pytest.raises(SystemExit):
        smoke.parse_args(["--confirm-readonly", "--operation", "add_words"])
    with pytest.raises(SystemExit):
        smoke.parse_args(
            ["--confirm-readonly", "--resource-id", "get_markji_deck.unknown=value"]
        )
    for unsafe in ("contains space", "bad\x7fvalue", "bad\u2028value"):
        with pytest.raises(SystemExit):
            smoke.parse_args(
                [
                    "--confirm-readonly",
                    "--operation",
                    "get_markji_deck",
                    "--resource-id",
                    f"get_markji_deck.deck={unsafe}",
                ]
            )
    parser_errors = capsys.readouterr().err
    assert "contains space" not in parser_errors
    assert "bad\x7fvalue" not in parser_errors
    assert "bad\u2028value" not in parser_errors
    parsed = smoke.parse_args(["--confirm-readonly", "--operation", "get_vocabulary"])
    assert parsed.operations == ("get_vocabulary",)
    assert parsed.resource_ids == {}
    parsed = smoke.parse_args(
        [
            "--confirm-readonly",
            "--operation",
            "get_markji_chapter",
            "--resource-id",
            "get_markji_chapter.deck=deck-1",
            "--resource-id",
            "get_markji_chapter.chapter=chapter-1",
        ]
    )
    assert parsed.resource_ids == {
        ("get_markji_chapter", "deck"): "deck-1",
        ("get_markji_chapter", "chapter"): "chapter-1",
    }


async def test_live_registry_has_exactly_17_atomic_reads_and_no_upstream_write(
    tmp_path: Path,
) -> None:
    token = tmp_path / "token"
    key = tmp_path / "key"
    token.write_text("evaluation-fake-token", encoding="utf-8")
    key.write_text("evaluation-fake-key", encoding="utf-8")
    settings = Settings(
        database_url="postgresql+psycopg://evaluation:evaluation@127.0.0.1:1/evaluation",
        token_file=token,
        token_fingerprint_key_file=key,
    )
    async with Client(create_mcp_app(settings).sdk) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert len(tools) == 24
    assert ATOMIC_READS <= tools.keys()
    assert len(ATOMIC_READS) == 17
    assert all(tools[name].annotations.read_only_hint is True for name in ATOMIC_READS)
    assert not any(
        fragment in name
        for name in tools
        for fragment in ("add_word", "create_note", "update_note", "delete_phrase", "early_review")
    )


async def test_smoke_output_never_contains_payload_or_error_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    smoke = load_smoke_module()

    class FakeSettings:
        @staticmethod
        def load() -> object:
            return object()

    class FakeClient:
        def __init__(self, sdk: object) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
            if name == "get_vocabulary":
                return SimpleNamespace(
                    is_error=False,
                    structured_content={"data": {"items": ["personal-payload-secret"]}},
                )
            raise RuntimeError("Bearer personal-token upstream-private-body")

    monkeypatch.setattr(smoke, "Settings", FakeSettings)
    monkeypatch.setattr(smoke, "create_mcp_app", lambda settings: SimpleNamespace(sdk=object()))
    monkeypatch.setattr(smoke, "Client", FakeClient)
    code = await smoke.run(
        smoke.Arguments(operations=("get_vocabulary", "list_markji_folders"), resource_ids={})
    )
    output = capsys.readouterr().out
    assert code == 1
    assert "count=1" in output
    assert "personal-payload-secret" not in output
    assert "personal-token" not in output
    assert "upstream-private-body" not in output


async def test_missing_account_identifier_is_prerequisite_without_tool_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    smoke = load_smoke_module()
    calls: list[str] = []

    class FakeClient:
        def __init__(self, sdk: object) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
            calls.append(name)
            raise AssertionError("missing identifier operation must not call MCP")

    monkeypatch.setattr(smoke.Settings, "load", lambda: object())
    monkeypatch.setattr(smoke, "create_mcp_app", lambda settings: SimpleNamespace(sdk=object()))
    monkeypatch.setattr(smoke, "Client", FakeClient)
    code = await smoke.run(
        smoke.Arguments(operations=("get_markji_deck",), resource_ids={})
    )
    output = capsys.readouterr().out
    assert code == 1
    assert calls == []
    assert "operation=get_markji_deck status=PREREQUISITE" in output
    assert "required_fields=deck" in output


@pytest.mark.parametrize("failure", ["401", "429", "not_found", "exception"])
async def test_provided_identifier_failures_are_fail_not_prerequisite(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    smoke = load_smoke_module()
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeClient:
        def __init__(self, sdk: object) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
            calls.append((name, arguments))
            if failure == "exception":
                raise TimeoutError("private upstream body")
            return SimpleNamespace(
                is_error=True,
                structured_content={"private": failure},
                content=[f"private {failure}"],
            )

    monkeypatch.setattr(smoke.Settings, "load", lambda: object())
    monkeypatch.setattr(smoke, "create_mcp_app", lambda settings: SimpleNamespace(sdk=object()))
    monkeypatch.setattr(smoke, "Client", FakeClient)
    code = await smoke.run(
        smoke.Arguments(
            operations=("get_markji_deck",),
            resource_ids={("get_markji_deck", "deck"): "safe-deck-id"},
        )
    )
    output = capsys.readouterr().out
    assert code == 1
    assert calls == [("get_markji_deck", {"deck": "safe-deck-id"})]
    assert "status=FAIL" in output
    assert "PREREQUISITE" not in output
    assert failure not in output
    assert "private upstream body" not in output
