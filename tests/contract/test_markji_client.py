"""Pinned Markji contracts: catch wrong routes, mapping, models and lax schemas."""

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from pydantic import BaseModel, SecretStr, ValidationError

from maimemo_mcp.maimemo_client.errors import UpstreamSchemaError
from maimemo_mcp.maimemo_client.markji import (
    ListDecksRequest,
    ListFoldersRequest,
    MarkjiClient,
    QueryFilesRequest,
)
from maimemo_mcp.maimemo_client.models import (
    GetCardResponse,
    GetChapterResponse,
    GetDeckResponse,
    ListChaptersResponse,
    ListDecksResponse,
    ListFoldersResponse,
    MarkjiCard,
    MarkjiChapter,
    MarkjiChapterset,
    MarkjiDeck,
    MarkjiFile,
    MarkjiFolder,
    MarkjiFolderItem,
    MarkjiRootDeck,
    QueryFilesResponse,
)
from maimemo_mcp.maimemo_client.transport import MaimemoTransport

FIXTURES = json.loads(
    (Path(__file__).parents[1] / "fixtures/maimemo/markji/responses.json").read_text(
        encoding="utf-8"
    )
)
CASES = [
    (
        "list_folders",
        "GET",
        "/api/v1/markji/decks/folders",
        {},
        None,
        ListFoldersResponse,
        "folders",
    ),
    (
        "list_decks",
        "GET",
        "/api/v1/markji/decks",
        {"offset": "2", "limit": "3", "folder_id": "folder-synthetic"},
        None,
        ListDecksResponse,
        "total",
    ),
    ("get_deck", "GET", "/api/v1/markji/decks/deck-synthetic", {}, None, GetDeckResponse, "deck"),
    (
        "list_chapters",
        "GET",
        "/api/v1/markji/decks/deck-synthetic/chapters",
        {},
        None,
        ListChaptersResponse,
        "chapters",
    ),
    (
        "get_chapter",
        "GET",
        "/api/v1/markji/decks/deck-synthetic/chapters/chapter-synthetic",
        {},
        None,
        GetChapterResponse,
        "chapter.id",
    ),
    (
        "get_card",
        "GET",
        "/api/v1/markji/decks/deck-synthetic/cards/card-synthetic",
        {},
        None,
        GetCardResponse,
        "card",
    ),
    (
        "query_files",
        "POST",
        "/api/v1/markji/files/query",
        {},
        {"ids": ["file-synthetic"], "expires": 60},
        QueryFilesResponse,
        "files",
    ),
]


class NoWaitLimiter:
    async def acquire(self, token_fingerprint: str) -> None:
        pass


async def invoke(client: MarkjiClient, operation: str) -> BaseModel:
    if operation == "list_folders":
        return await client.list_folders(ListFoldersRequest())
    if operation == "list_decks":
        return await client.list_decks(
            ListDecksRequest(offset=2, limit=3, folder_id="folder-synthetic")
        )
    if operation == "query_files":
        return await client.query_files(QueryFilesRequest(ids=["file-synthetic"], expires=60))
    if operation == "get_deck":
        return await client.get_deck("deck-synthetic")
    if operation == "list_chapters":
        return await client.list_chapters("deck-synthetic")
    if operation == "get_chapter":
        return await client.get_chapter("deck-synthetic", "chapter-synthetic")
    return await client.get_card("deck-synthetic", "card-synthetic")


@pytest.mark.parametrize("dot,encoded", [(".", "%2E"), ("..", "%2E%2E")])
@pytest.mark.parametrize("operation,position", [
    ("get_deck", 0), ("list_chapters", 0),
    ("get_chapter", 0), ("get_chapter", 1), ("get_card", 0), ("get_card", 1),
])
async def test_exact_dot_ids_retain_the_final_markji_operation_path(
    dot: str, encoded: str, operation: str, position: int,
) -> None:
    ids = ["deck-synthetic"]
    if operation in ("get_chapter", "get_card"):
        ids.append("chapter-synthetic" if operation == "get_chapter" else "card-synthetic")
    ids[position] = dot
    wanted_ids = list(ids)
    wanted_ids[position] = encoded
    wanted = "/open/api/v1/markji/decks/" + wanted_ids[0]
    if operation == "list_chapters":
        wanted += "/chapters"
    if operation in ("get_chapter", "get_card"):
        wanted += ("/chapters/" if operation == "get_chapter" else "/cards/") + wanted_ids[1]
    paths = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.raw_path)
        return httpx.Response(200, json=FIXTURES[operation])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        transport = MaimemoTransport(SecretStr("synthetic-token"), SecretStr("synthetic-key"),
                                    NoWaitLimiter(), client=http)
        await getattr(MarkjiClient(transport), operation)(*ids)
    assert paths == [wanted.encode("ascii")]


@pytest.mark.parametrize("operation,method,path,query,body,model,missing", CASES)
async def test_operation_contract(
    operation: str,
    method: str,
    path: str,
    query: dict[str, str],
    body: dict[str, Any] | None,
    model: type[BaseModel],
    missing: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = copy.deepcopy(FIXTURES[operation])
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == method
        assert request.url.path == "/open" + path
        assert request.url.host == "open.maimemo.com"
        assert dict(request.url.params) == query
        assert (json.loads(request.content) if request.content else None) == body
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
        client = MarkjiClient(transport)
        result = await invoke(client, operation)
        assert type(result) is model
        assert result.model_dump(exclude_unset=True) == payload
        assert len(calls) == 1
        payload = copy.deepcopy(FIXTURES["unknown_optional"][operation])
        assert (await invoke(client, operation)).model_extra == {
            "future_optional": {"synthetic": True}
        }
        payload = copy.deepcopy(FIXTURES["empty"].get(operation, FIXTURES[operation]))
        assert (await invoke(client, operation)).model_dump(exclude_unset=True) == payload
        payload = copy.deepcopy(FIXTURES["missing_required"][operation])
        with pytest.raises(UpstreamSchemaError):
            await invoke(client, operation)
        assert len(calls) == 4  # No endpoint retry on malformed successful responses.


def test_request_contract_rejects_missing_ids_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        QueryFilesRequest()  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ListFoldersRequest(folder_id="synthetic")
    with pytest.raises(ValidationError):
        ListDecksRequest(limit="3")


def test_nested_required_fields_and_enums_are_validated() -> None:
    for model, payload, key in [
        (ListFoldersResponse, FIXTURES["list_folders"], "folders"),
        (ListDecksResponse, FIXTURES["list_decks"], "decks"),
        (ListChaptersResponse, FIXTURES["list_chapters"], "chapters"),
        (QueryFilesResponse, FIXTURES["query_files"], "files"),
    ]:
        malformed = copy.deepcopy(payload)
        del malformed[key][0]["id"]
        with pytest.raises(ValidationError):
            model.model_validate(malformed)
    malformed = copy.deepcopy(FIXTURES["get_card"])
    malformed["card"]["status"] = "INVENTED"
    with pytest.raises(ValidationError):
        GetCardResponse.model_validate(malformed)


@pytest.mark.parametrize(
    "model,payload",
    [
        (MarkjiRootDeck, FIXTURES["get_deck"]["deck"]),
        (MarkjiDeck, FIXTURES["get_deck"]["deck"]),
        (MarkjiChapterset, FIXTURES["list_chapters"]["chapterset"]),
        (MarkjiChapter, FIXTURES["get_chapter"]["chapter"]),
        (MarkjiCard, FIXTURES["get_card"]["card"]),
        (MarkjiFile, FIXTURES["query_files"]["files"][0]),
        (MarkjiFolder, FIXTURES["list_folders"]["folders"][0]),
        (MarkjiFolderItem, FIXTURES["list_folders"]["folders"][0]["items"][0]),
    ],
)
def test_each_pinned_required_resource_field_is_enforced(
    model: type[BaseModel],
    payload: dict[str, Any],
) -> None:
    # Independently derive omissions from the pinned specification, never model metadata.
    spec = yaml.safe_load(
        (Path(__file__).parents[2] / "openapi/maimemo-api.yaml").read_text(encoding="utf-8")
    )
    model.model_validate(payload)
    extended = dict(payload, future_optional="synthetic")
    assert model.model_validate(extended).model_extra == {"future_optional": "synthetic"}
    for required in spec["components"]["schemas"][model.__name__]["required"]:
        malformed = copy.deepcopy(payload)
        del malformed[required]
        with pytest.raises(ValidationError):
            model.model_validate(malformed)


PINNED_SPEC = yaml.safe_load(
    (Path(__file__).parents[2] / "openapi/maimemo-api.yaml").read_text(encoding="utf-8")
)
OPTIONAL_SHAPES = [
    (
        MarkjiRootDeck,
        FIXTURES["get_deck"]["deck"],
        PINNED_SPEC["components"]["schemas"]["MarkjiRootDeck"],
    ),
    (MarkjiDeck, FIXTURES["get_deck"]["deck"], PINNED_SPEC["components"]["schemas"]["MarkjiDeck"]),
    (MarkjiCard, FIXTURES["get_card"]["card"], PINNED_SPEC["components"]["schemas"]["MarkjiCard"]),
    (
        MarkjiFolder,
        FIXTURES["list_folders"]["folders"][0],
        PINNED_SPEC["components"]["schemas"]["MarkjiFolder"],
    ),
    (
        ListChaptersResponse,
        FIXTURES["list_chapters"],
        PINNED_SPEC["paths"]["/api/v1/markji/decks/{deck}/chapters"]["get"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"],
    ),
    (
        GetChapterResponse,
        FIXTURES["get_chapter"],
        PINNED_SPEC["paths"]["/api/v1/markji/decks/{deck}/chapters/{chapter}"]["get"]["responses"][
            "200"
        ]["content"]["application/json"]["schema"],
    ),
]


@pytest.mark.parametrize(
    "model,payload,field",
    [
        pytest.param(model, payload, field, id=f"{model.__name__}.{field}")
        for model, payload, schema in OPTIONAL_SHAPES
        for field, field_schema in schema["properties"].items()
        if field not in schema["required"]
        and field_schema["type"] in ("string", "array", "object")
        and not field_schema.get("nullable", False)
    ],
)
def test_pinned_optional_nonnullable_field_can_be_omitted_but_cannot_be_null(
    model: type[BaseModel],
    payload: dict[str, Any],
    field: str,
) -> None:
    omitted = copy.deepcopy(payload)
    omitted.pop(field, None)
    assert field not in model.model_validate(omitted).model_dump(exclude_unset=True)
    with pytest.raises(ValidationError):
        model.model_validate(dict(omitted, **{field: None}))


def test_unknown_optional_null_is_preserved() -> None:
    assert GetChapterResponse.model_validate({"future_optional": None}).model_extra == {
        "future_optional": None
    }
