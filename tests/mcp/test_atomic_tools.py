"""Real MCP and API clients; replace only the external HTTP boundary.

Literal cases catch missing tools, wrong endpoints/arguments, payload loss and local writes.
"""

import asyncio
import copy
import json
import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from mcp.client import Client
from sqlalchemy import text
from sqlalchemy.engine import make_url

from maimemo_mcp.config import Settings
from maimemo_mcp.maimemo_client.markji import MarkjiClient
from maimemo_mcp.maimemo_client.memo_content import MemoContentClient
from maimemo_mcp.maimemo_client.study import StudyClient
from maimemo_mcp.mcp_server.app import MCPServer, create_mcp_app

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

FIXTURES = Path(__file__).parents[1] / "fixtures/maimemo"
MARKJI = json.loads((FIXTURES / "markji/responses.json").read_text("utf-8"))
MEMO = json.loads((FIXTURES / "memo_content/responses.json").read_text("utf-8"))
STUDY = json.loads((FIXTURES / "study/responses.json").read_text("utf-8"))
AT = datetime(2026, 10, 2, 16, tzinfo=timezone(timedelta(hours=8)))

# Independent method expectations; spies delegate to the real clients/transport/limiter.
CLIENT_METHODS = {
    "list_markji_folders": (MarkjiClient, "markji", "list_folders"),
    "list_markji_decks": (MarkjiClient, "markji", "list_decks"),
    "get_markji_deck": (MarkjiClient, "markji", "get_deck"),
    "list_markji_chapters": (MarkjiClient, "markji", "list_chapters"),
    "get_markji_chapter": (MarkjiClient, "markji", "get_chapter"),
    "get_markji_card": (MarkjiClient, "markji", "get_card"),
    "query_markji_files": (MarkjiClient, "markji", "query_files"),
    "get_interpretations": (MemoContentClient, "memo_content", "get_interpretations"),
    "get_notes": (MemoContentClient, "memo_content", "get_notes"),
    "list_notepads": (MemoContentClient, "memo_content", "list_notepads"),
    "get_notepad": (MemoContentClient, "memo_content", "get_notepad"),
    "get_phrases": (MemoContentClient, "memo_content", "get_phrases"),
    "get_vocabulary": (MemoContentClient, "memo_content", "get_vocabulary"),
    "query_vocabulary": (MemoContentClient, "memo_content", "query_vocabulary"),
    "get_study_progress": (StudyClient, "study", "get_progress"),
    "get_today_items": (StudyClient, "study", "get_today_items"),
    "query_study_records": (StudyClient, "study", "query_records"),
}

# tool, arguments, method, endpoint, query/body, response fixture
CASES = [
    ("list_markji_folders", {}, "GET", "/markji/decks/folders", {}, MARKJI["list_folders"]),
    (
        "list_markji_decks",
        {"request": {"offset": 2, "limit": 3, "folder_id": "folder-id"}},
        "GET",
        "/markji/decks",
        {"offset": "2", "limit": "3", "folder_id": "folder-id"},
        MARKJI["list_decks"],
    ),
    (
        "get_markji_deck",
        {"deck": "deck-id"},
        "GET",
        "/markji/decks/deck-id",
        {},
        MARKJI["get_deck"],
    ),
    (
        "list_markji_chapters",
        {"deck": "deck-id"},
        "GET",
        "/markji/decks/deck-id/chapters",
        {},
        MARKJI["list_chapters"],
    ),
    (
        "get_markji_chapter",
        {"deck": "deck-id", "chapter": "chapter-id"},
        "GET",
        "/markji/decks/deck-id/chapters/chapter-id",
        {},
        MARKJI["get_chapter"],
    ),
    (
        "get_markji_card",
        {"deck": "deck-id", "card": "card-id"},
        "GET",
        "/markji/decks/deck-id/cards/card-id",
        {},
        MARKJI["get_card"],
    ),
    (
        "query_markji_files",
        {"request": {"ids": ["file-id"], "expires": 60}},
        "POST",
        "/markji/files/query",
        {"ids": ["file-id"], "expires": 60},
        MARKJI["query_files"],
    ),
    (
        "get_interpretations",
        {"voc_id": "word-id"},
        "GET",
        "/memo/interpretations",
        {"voc_id": "word-id"},
        MEMO["valid"]["get_interpretations"],
    ),
    (
        "get_notes",
        {"voc_id": "word-id"},
        "GET",
        "/memo/notes",
        {"voc_id": "word-id"},
        MEMO["valid"]["get_notes"],
    ),
    (
        "list_notepads",
        {"request": {"limit": 4, "offset": 1, "ids": ["pad-a", "pad-b"]}},
        "GET",
        "/memo/notepads",
        {"limit": "4", "offset": "1", "ids": ["pad-a", "pad-b"]},
        MEMO["valid"]["list_notepads"],
    ),
    (
        "get_notepad",
        {"notepad_id": "pad-id"},
        "GET",
        "/memo/notepads/pad-id",
        {},
        MEMO["valid"]["get_notepad"],
    ),
    (
        "get_phrases",
        {"voc_id": "word-id"},
        "GET",
        "/memo/phrases",
        {"voc_id": "word-id"},
        MEMO["valid"]["get_phrases"],
    ),
    (
        "get_study_progress",
        {},
        "POST",
        "/memo/study/get_study_progress",
        None,
        STUDY["complete"]["get_progress"],
    ),
    (
        "get_today_items",
        {"request": {"voc_ids": ["word-id"], "limit": 1000, "is_finished": False, "is_new": True}},
        "POST",
        "/memo/study/get_today_items",
        {"voc_ids": ["word-id"], "limit": 1000, "is_finished": False, "is_new": True},
        STUDY["schema_drift"]["get_today_items"],
    ),
    (
        "query_study_records",
        {
            "request": {
                "spellings": [" Apple "],
                "limit": 1000,
                "as_count": False,
                "tags": "STICKING",
                "next_study_date": {"end": "2026-10-03"},
            }
        },
        "POST",
        "/memo/study/query_study_records",
        {
            "spellings": [" Apple "],
            "limit": 1000,
            "as_count": False,
            "tags": "STICKING",
            "next_study_date": {"end": "2026-10-03"},
        },
        STUDY["schema_drift"]["query_records"],
    ),
    (
        "get_vocabulary",
        {"spelling": " Apple "},
        "GET",
        "/memo/vocabulary",
        {"spelling": " Apple "},
        MEMO["unknown_optional"]["get_vocabulary"],
    ),
    (
        "query_vocabulary",
        {"request": {"ids": ["word-id"]}},
        "POST",
        "/memo/vocabulary/query",
        {"ids": ["word-id"]},
        MEMO["unknown_optional"]["query_vocabulary"],
    ),
]


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    url = os.environ.get(
        "MAIMEMO_TEST_DATABASE_URL",
        "postgresql+psycopg://maimemo_test:test_only@127.0.0.1:55432/maimemo_test",
    )
    if not (make_url(url).database or "").endswith("_test"):
        raise ValueError("Atomic tests require a disposable database ending in _test")
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")
    try:
        yield url
    finally:
        command.downgrade(config, "base")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    token, key = tmp_path / "token", tmp_path / "key"
    token.write_text("test-token-only", encoding="utf-8")
    key.write_text("test-key-only", encoding="utf-8")
    return Settings(
        database_url="postgresql+psycopg://test_only:test_only@127.0.0.1:1/atomic_test",
        token_file=token,
        token_fingerprint_key_file=key,
    )


async def test_exact_inventory_safety_and_explicit_schemas(settings: Settings) -> None:
    server = create_mcp_app(settings)
    async with Client(server.sdk) as client:
        tools = (await client.list_tools()).tools
    assert {tool.name for tool in tools} == {
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
        "get_daily_study_dashboard",
        "get_word_learning_profile",
        "get_weak_words",
        "get_due_review_overview",
        "get_learning_data_health",
        "record_confusion_feedback",
        "retract_feedback",
    }
    assert len(tools) == 24
    for tool in tools:
        assert tool.annotations is not None
        feedback = tool.name in ("record_confusion_feedback", "retract_feedback")
        assert tool.annotations.read_only_hint is (not feedback)
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.idempotent_hint is True
        live = tool.name in CLIENT_METHODS or tool.name == "get_word_learning_profile"
        assert tool.annotations.open_world_hint is live
        assert tool.description
        assert tool.input_schema["type"] == "object"
        assert "properties" in tool.input_schema
        assert "ctx" not in tool.input_schema["properties"]
        assert tool.output_schema is not None
    schemas = {tool.name: tool.input_schema for tool in tools}
    assert schemas["get_vocabulary"]["required"] == ["spelling"]
    assert schemas["get_notes"]["required"] == ["voc_id"]
    for name, model in (
        ("get_today_items", "TodayItemsRequest"),
        ("query_study_records", "StudyRecordsRequest"),
        ("query_vocabulary", "QueryVocabularyRequest"),
    ):
        props = schemas[name]["$defs"][model]["properties"]
        assert props["spellings"]["anyOf"][0]["maxItems"] == 1000
        if name != "query_vocabulary":
            assert props["limit"]["anyOf"][0]["maximum"] == 1000
    records = schemas["query_study_records"]["$defs"]["StudyRecordsRequest"]["properties"]
    assert "maxItems" not in records["voc_ids"]["anyOf"][0]


async def row_counts(server: MCPServer) -> tuple[int, ...]:
    assert server.dependencies is not None
    async with server.dependencies.engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT (SELECT count(*) FROM api_snapshot), "
                "(SELECT count(*) FROM daily_progress), "
                "(SELECT count(*) FROM daily_word_observation), "
                "(SELECT count(*) FROM study_record_snapshot), "
                "(SELECT count(*) FROM learning_feedback_event), "
                "(SELECT count(*) FROM vocabulary), (SELECT count(*) FROM ingestion_run)"
            )
        )
        return tuple(int(value) for value in result.one())


@pytest.mark.parametrize(
    "name,args,payload",
    [
        ("get_study_progress", {}, {"progress": {"finished": 0, "total": 0, "study_time": 0}}),
        ("get_today_items", {"request": {}}, {"today_items": []}),
        ("query_study_records", {"request": {"as_count": True}}, {"records": [], "count": 0}),
    ],
)
async def test_empty_live_study_never_claims_initialized_or_complete_history(
    settings: Settings,
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    args: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    real_client = httpx.AsyncClient

    def make_client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
            **kwargs,
        )

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    server = create_mcp_app(settings.model_copy(update={"database_url": postgres_url}))
    before_time = datetime.now(UTC)
    async with Client(server.sdk) as client:
        before = await row_counts(server)
        result = await client.call_tool(name, args)
        assert result.is_error is False
        assert result.structured_content is not None
        assert result.structured_content["data"] == payload
        meta = result.structured_content["meta"]
        assert meta["completeness"] == "partial"
        assert meta["data_through"] is None
        assert meta["warnings"] == ["live_scope_only"]
        assert before_time <= datetime.fromisoformat(meta["fetched_at"]) <= datetime.now(UTC)
        assert await row_counts(server) == before == (0, 0, 0, 0, 0, 0, 0)


@pytest.mark.parametrize("status", [401, 422, 200])
async def test_upstream_failures_use_sdk_errors_without_secret_or_local_writes(
    settings: Settings,
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    real_client = httpx.AsyncClient

    def make_client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    status, json={"error": "Bearer test-token-only personal-history"}
                )
            ),
            **kwargs,
        )

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    server = create_mcp_app(settings.model_copy(update={"database_url": postgres_url}))
    async with Client(server.sdk) as client:
        before = await row_counts(server)
        result = await client.call_tool("get_notes", {"voc_id": "word-id"})
        assert result.is_error is True
        assert result.structured_content is None
        assert "test-token-only" not in str(result.content)
        assert "personal-history" not in str(result.content)
        assert await row_counts(server) == before == (0, 0, 0, 0, 0, 0, 0)


@pytest.mark.parametrize(
    "name,inputs",
    [
        ("get_today_items", {"limit": 1001}),
        ("query_study_records", {"spellings": ["word"] * 1001}),
        ("query_vocabulary", {"ids": ["word-id"] * 1001}),
        ("get_today_items", {"voc_ids": ["id"], "spellings": ["word"]}),
        ("query_study_records", {"voc_ids": ["id"], "spellings": ["word"]}),
        ("query_vocabulary", {"ids": ["id"], "spellings": ["word"]}),
    ],
)
async def test_request_models_reject_invalid_conditions_before_http(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    inputs: dict[str, Any],
) -> None:
    calls: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    def make_client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    server = create_mcp_app(settings)
    async with Client(server.sdk) as client:
        result = await client.call_tool(name, {"request": inputs})
        assert result.is_error is True
        assert calls == []


@pytest.mark.parametrize(
    "name,args,method,path,expected,payload", CASES, ids=[case[0] for case in CASES]
)
async def test_atomic_mapping_live_envelope_and_zero_local_rows(
    settings: Settings,
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    args: dict[str, Any],
    method: str,
    path: str,
    expected: dict[str, Any] | None,
    payload: dict[str, Any],
) -> None:
    calls: list[httpx.Request] = []
    method_calls: list[tuple[Any, tuple[Any, ...]]] = []
    client_class, dependency_name, method_name = CLIENT_METHODS[name]
    original = getattr(client_class, method_name)

    async def observed(upstream_client: Any, *method_args: Any) -> Any:
        method_calls.append((upstream_client, method_args))
        return await original(upstream_client, *method_args)

    monkeypatch.setattr(client_class, method_name, observed)

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == method
        assert str(request.url).split("?", 1)[0] == "https://open.maimemo.com/open/api/v1" + path
        if method == "GET":
            actual: Any = {
                key: request.url.params.get_list(key) if key == "ids" else request.url.params[key]
                for key in request.url.params
            }
        else:
            actual = json.loads(request.content) if request.content else None
        assert actual == expected
        return httpx.Response(200, json=payload)

    real_client = httpx.AsyncClient

    def make_client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    settings = settings.model_copy(update={"database_url": postgres_url})
    if name in ("get_today_items", "query_study_records"):
        payload = copy.deepcopy(payload)
        if name == "get_today_items":
            payload["today_items"][0]["first_response"] = "Bearer test-token-only"
        else:
            payload["records"][0]["last_response"] = "Bearer test-token-only"

    # The clock is sampled after upstream success, not at tool entry.
    def clock() -> datetime:
        assert len(calls) == 1
        return AT

    server = create_mcp_app(settings, clock=clock)
    async with Client(server.sdk) as client:
        before = await row_counts(server)
        result = await client.call_tool(name, args)
        assert result.is_error is False, result.content
        assert server.dependencies is not None
        assert len(method_calls) == 1
        upstream_client, forwarded = method_calls[0]
        assert upstream_client is getattr(server.dependencies, dependency_name)
        if name == "list_markji_folders":
            assert len(forwarded) == 1
            assert forwarded[0].model_dump() == {}
        elif "request" in args:
            assert len(forwarded) == 1
            assert forwarded[0].model_dump(exclude_unset=True) == args["request"]
        else:
            assert forwarded == tuple(args.values())
        assert result.structured_content is not None
        # SDK serializes the live Pydantic response, including omitted optional defaults.
        want = copy.deepcopy(payload)
        if name == "list_markji_folders":
            want["folders"][0]["parent_id"] = None
        elif name in ("list_markji_decks", "get_markji_deck"):
            deck = want["decks"][0] if name == "list_markji_decks" else want["deck"]
            deck.update(parent_id=None, root_deck=None)
        elif name == "get_markji_chapter":
            want["chapterset"] = None
        elif name == "get_markji_card":
            want["card"].update(parent_id=None, root_id=None, card_rids=None)
        elif name == "query_study_records":
            want["records"][0].update(
                first_study_date=None, last_study_date=None, next_study_date=None
            )
        assert result.structured_content["data"] == want
        warning = name in ("get_today_items", "query_study_records")
        assert result.structured_content["meta"] == {
            "source": ["maimemo_api"],
            "fetched_at": "2026-10-02T08:00:00Z",
            "data_through": None,
            "completeness": "partial",
            "warnings": ["live_scope_only"] + (["unknown_study_response"] if warning else []),
        }
        assert await row_counts(server) == before == (0, 0, 0, 0, 0, 0, 0)
        assert len(calls) == 1
        assert datetime.fromisoformat(result.structured_content["meta"]["fetched_at"]).tzinfo == UTC
