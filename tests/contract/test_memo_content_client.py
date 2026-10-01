"""Synthetic pinned contracts catch route, placement, limits and schema regressions."""

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from pydantic import BaseModel, SecretStr, ValidationError

from maimemo_mcp.maimemo_client import models
from maimemo_mcp.maimemo_client.errors import UpstreamSchemaError
from maimemo_mcp.maimemo_client.memo_content import (
    ListNotepadsRequest,
    MemoContentClient,
    QueryVocabularyRequest,
)
from maimemo_mcp.maimemo_client.transport import MaimemoTransport

FIXTURES = json.loads(
    (Path(__file__).parents[1] / "fixtures/maimemo/memo_content/responses.json").read_text(
        encoding="utf-8"
    )
)
SPEC = yaml.safe_load(
    (Path(__file__).parents[2] / "openapi/maimemo-api.yaml").read_text(encoding="utf-8")
)
CASES = [
    ("get_interpretations", "GET", "/api/v1/memo/interpretations", "InterpretationsResponse"),
    ("get_notes", "GET", "/api/v1/memo/notes", "NotesResponse"),
    ("list_notepads", "GET", "/api/v1/memo/notepads", "ListNotepadsResponse"),
    ("get_notepad", "GET", "/api/v1/memo/notepads/notepad-synthetic", "GetNotepadResponse"),
    ("get_phrases", "GET", "/api/v1/memo/phrases", "PhrasesResponse"),
    ("get_vocabulary", "GET", "/api/v1/memo/vocabulary", "VocabularyResponse"),
    ("query_vocabulary", "POST", "/api/v1/memo/vocabulary/query", "QueryVocabularyResponse"),
]


class NoWaitLimiter:
    async def acquire(self, token_fingerprint: str) -> None:
        pass


async def invoke(client: MemoContentClient, operation: str) -> BaseModel:
    if operation == "list_notepads":
        return await client.list_notepads(
            ListNotepadsRequest(limit=3, offset=2, ids=["pad-a", "pad-b"])
        )
    if operation == "get_notepad":
        return await client.get_notepad("notepad-synthetic")
    if operation == "query_vocabulary":
        return await client.query_vocabulary(QueryVocabularyRequest(spellings=[" Apple "]))
    return await getattr(client, operation)(
        " Apple " if operation == "get_vocabulary" else "voc-synthetic"
    )


@pytest.mark.parametrize("operation,method,path,model_name", CASES)
async def test_operation_contract(
    operation: str, method: str, path: str, model_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = FIXTURES["valid"][operation]
    calls: list[httpx.Request] = []
    model = getattr(models, model_name)

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == method
        assert str(request.url).split("?")[0] == "https://open.maimemo.com/open" + path
        query = list(request.url.params.multi_items())
        if operation == "list_notepads":
            assert query == [("limit", "3"), ("offset", "2"), ("ids", "pad-a"), ("ids", "pad-b")]
        elif operation == "get_vocabulary":
            assert query == [("spelling", " Apple ")]
        elif operation in ("get_interpretations", "get_notes", "get_phrases"):
            assert query == [("voc_id", "voc-synthetic")]
        else:
            assert query == []
        assert (json.loads(request.content) if request.content else None) == (
            {"spellings": [" Apple "]} if operation == "query_vocabulary" else None
        )
        calls.append(request)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        transport = MaimemoTransport(
            SecretStr("synthetic-token"), SecretStr("synthetic-key"), NoWaitLimiter(), client=http
        )
        original = transport.request

        async def capture(*args: Any, **kwargs: Any) -> Any:
            assert args == (method, path)
            assert kwargs["response_type"] is model
            return await original(*args, **kwargs)

        monkeypatch.setattr(transport, "request", capture)
        client = MemoContentClient(transport)
        for variant in ("valid", "empty", "unknown_optional"):
            payload = FIXTURES[variant][operation]
            result = await invoke(client, operation)
            assert type(result) is model
            assert result.model_dump(exclude_unset=True) == payload
        payload = FIXTURES["missing_required"][operation]
        with pytest.raises(UpstreamSchemaError):
            await invoke(client, operation)
        assert len(calls) == 4


@pytest.mark.parametrize("field", ["spellings", "ids"])
def test_batch_maximum_is_enforced_without_inventing_minimum(field: str) -> None:
    assert QueryVocabularyRequest(**{field: []}).model_dump(exclude_none=True) == {field: []}
    assert len(getattr(QueryVocabularyRequest(**{field: ["synthetic"] * 1000}), field)) == 1000
    with pytest.raises(ValidationError):
        QueryVocabularyRequest(**{field: ["synthetic"] * 1001})


def test_request_optional_and_exclusive_conditions() -> None:
    assert QueryVocabularyRequest().model_dump(exclude_none=True) == {}
    assert ListNotepadsRequest().model_dump(exclude_none=True) == {}
    assert ListNotepadsRequest(limit=-1, offset=-1, ids=[""] * 1001).limit == -1
    with pytest.raises(ValidationError):
        QueryVocabularyRequest(ids=["synthetic"], spellings=["apple"])
    for request, values in [
        (QueryVocabularyRequest, {"ids": [1]}),
        (QueryVocabularyRequest, {"invented": True}),
        (ListNotepadsRequest, {"limit": "3"}),
        (ListNotepadsRequest, {"invented": True}),
    ]:
        with pytest.raises(ValidationError):
            request(**values)


RESOURCE_CASES = [
    ("Interpretation", FIXTURES["valid"]["get_interpretations"]["interpretations"][0]),
    ("Note", FIXTURES["valid"]["get_notes"]["notes"][0]),
    ("BriefNotepad", FIXTURES["valid"]["list_notepads"]["notepads"][0]),
    ("Notepad", FIXTURES["valid"]["get_notepad"]["notepad"]),
    ("NotepadParsedItem", FIXTURES["valid"]["get_notepad"]["notepad"]["list"][0]),
    ("Phrase", FIXTURES["valid"]["get_phrases"]["phrases"][0]),
    ("PhraseHighlightRange", {"start": 11, "end": 16}),
    ("Vocabulary", FIXTURES["valid"]["get_vocabulary"]["voc"]),
]


@pytest.mark.parametrize("name,payload", RESOURCE_CASES)
def test_resource_required_types_enums_and_extensions(name: str, payload: dict[str, Any]) -> None:
    model = getattr(models, name)
    schema = SPEC["components"]["schemas"][name]
    assert model.model_validate(payload).model_dump(exclude_unset=True) == payload
    assert model.model_validate(dict(payload, future_optional=None)).model_extra == {
        "future_optional": None
    }
    for field in schema["required"]:
        malformed = copy.deepcopy(payload)
        del malformed[field]
        with pytest.raises(ValidationError):
            model.model_validate(malformed)
        with pytest.raises(ValidationError):
            model.model_validate(dict(payload, **{field: None}))
    for field, field_schema in schema["properties"].items():
        if "$ref" in field_schema:
            field_schema = SPEC["components"]["schemas"][field_schema["$ref"].split("/")[-1]]
        if "enum" in field_schema:
            with pytest.raises(ValidationError):
                model.model_validate(dict(payload, **{field: "INVENTED"}))


def test_parsed_item_optional_word_is_nonnullable_and_chapter_required() -> None:
    model = models.NotepadParsedItem
    assert model.model_validate({"type": "CHAPTER", "data": {"chapter": "synthetic"}})
    for data in ({"chapter": "synthetic", "word": None}, {"word": "apple"}):
        with pytest.raises(ValidationError):
            model.model_validate({"type": "WORD", "data": data})


async def test_optional_request_fields_are_omitted_and_ids_body_preserved() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"notepads": []} if request.method == "GET" else {"voc": []}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = MemoContentClient(
            MaimemoTransport(
                SecretStr("synthetic-token"),
                SecretStr("synthetic-key"),
                NoWaitLimiter(),
                client=http,
            )
        )
        await client.list_notepads(ListNotepadsRequest())
        await client.query_vocabulary(QueryVocabularyRequest())
        await client.query_vocabulary(QueryVocabularyRequest(ids=["voc-synthetic"]))
    assert str(requests[0].url) == "https://open.maimemo.com/open/api/v1/memo/notepads"
    assert json.loads(requests[1].content) == {}
    assert json.loads(requests[2].content) == {"ids": ["voc-synthetic"]}


@pytest.mark.parametrize(
    "operation",
    ["get_interpretations", "get_notes", "get_phrases", "get_vocabulary", "get_notepad"],
)
@pytest.mark.parametrize("value", [None, 12])
async def test_required_string_parameter_rejected_before_network(
    operation: str, value: Any
) -> None:
    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid required string must not issue an HTTP request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as http:
        client = MemoContentClient(
            MaimemoTransport(
                SecretStr("synthetic-token"),
                SecretStr("synthetic-key"),
                NoWaitLimiter(),
                client=http,
            )
        )
        with pytest.raises(ValidationError):
            await getattr(client, operation)(value)


@pytest.mark.parametrize(
    "notepad_id,encoded_id",
    [
        (".", "%2E"),
        ("..", "%2E%2E"),
        ("notepad-synthetic", "notepad-synthetic"),
        ("pad/name", "pad%2Fname"),
        ("pad name", "pad%20name"),
        ("词本", "%E8%AF%8D%E6%9C%AC"),
        ("pad..name", "pad..name"),
    ],
)
async def test_notepad_id_stays_in_endpoint_segment_on_wire(
    notepad_id: str, encoded_id: str
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=FIXTURES["valid"]["get_notepad"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = MemoContentClient(
            MaimemoTransport(
                SecretStr("synthetic-token"),
                SecretStr("synthetic-key"),
                NoWaitLimiter(),
                client=http,
            )
        )
        result = await client.get_notepad(notepad_id)
    assert result.notepad.id == "notepad-synthetic"
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == (
        "https://open.maimemo.com/open/api/v1/memo/notepads/" + encoded_id
    )
    assert requests[0].url.raw_path == ("/open/api/v1/memo/notepads/" + encoded_id).encode("ascii")
