"""Synthetic study contracts catch wrong routes, data loss and schema weakening."""

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from pydantic import SecretStr, ValidationError

from maimemo_mcp.maimemo_client import models
from maimemo_mcp.maimemo_client.errors import UpstreamSchemaError
from maimemo_mcp.maimemo_client.study import (
    StudyClient,
    StudyDateRange,
    StudyRecordsRequest,
    TodayItemsRequest,
)
from maimemo_mcp.maimemo_client.transport import MaimemoTransport

FIXTURES = json.loads(
    (Path(__file__).parents[1] / "fixtures/maimemo/study/responses.json").read_text("utf-8")
)
SPEC = yaml.safe_load((Path(__file__).parents[2] / "openapi/maimemo-api.yaml").read_text("utf-8"))
CASES = [
    ("get_progress", "get_study_progress", "StudyProgressResponse"),
    ("get_today_items", "get_today_items", "TodayItemsResponse"),
    ("query_records", "query_study_records", "StudyRecordsResponse"),
]


class NoWaitLimiter:
    async def acquire(self, token_fingerprint: str) -> None:
        pass


async def invoke(client: StudyClient, operation: str) -> Any:
    if operation == "get_progress":
        return await client.get_progress()
    if operation == "get_today_items":
        return await client.get_today_items(
            TodayItemsRequest(is_finished=False, is_new=True, voc_ids=["a", "b"], limit=1000)
        )
    return await client.query_records(
        StudyRecordsRequest(
            next_study_date=StudyDateRange(end="2026-10-03T00:00:00+08:00"),
            spellings=[" Apple "],
            tags="STICKING",
            as_count=False,
            limit=1000,
        )
    )


@pytest.mark.parametrize("operation,endpoint,model_name", CASES)
async def test_operation_contract(operation: str, endpoint: str, model_name: str) -> None:
    payload = FIXTURES["complete"][operation]
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "POST"
        assert str(request.url) == "https://open.maimemo.com/open/api/v1/memo/study/" + endpoint
        assert not request.url.query
        body = json.loads(request.content) if request.content else None
        expected = {
            "get_progress": None,
            "get_today_items": {
                "is_finished": False,
                "is_new": True,
                "voc_ids": ["a", "b"],
                "limit": 1000,
            },
            "query_records": {
                "next_study_date": {"end": "2026-10-03T00:00:00+08:00"},
                "spellings": [" Apple "],
                "tags": "STICKING",
                "as_count": False,
                "limit": 1000,
            },
        }
        assert body == expected[operation]
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = StudyClient(
            MaimemoTransport(
                SecretStr("synthetic-token"), SecretStr("key"), NoWaitLimiter(), client=http
            )
        )
        for variant in ("complete", "empty", "uninitialized", "schema_drift"):
            payload = FIXTURES[variant][operation]
            result = await invoke(client, operation)
            assert type(result) is getattr(models, model_name)
            assert result.model_dump(exclude_unset=True) == payload
        payload = {}
        with pytest.raises(UpstreamSchemaError):
            await invoke(client, operation)
        assert len(calls) == 5


@pytest.mark.parametrize("variant", ["empty", "uninitialized"])
def test_empty_today_items_is_valid_but_not_proof_of_initialization(variant: str) -> None:
    result = models.TodayItemsResponse.model_validate(FIXTURES[variant]["get_today_items"])
    assert result.today_items == []
    assert result.parsing_warnings == []
    assert result.model_dump(exclude_unset=True) == {"today_items": []}
    assert not hasattr(result, "completeness")
    assert not hasattr(result, "initialized")


@pytest.mark.parametrize(
    "model_name,operation,path",
    [
        ("TodayItemsResponse", "get_today_items", "today_items.0.first_response"),
        ("StudyRecordsResponse", "query_records", "records.0.last_response"),
    ],
)
def test_unknown_study_response_is_preserved_and_warned(
    model_name: str, operation: str, path: str
) -> None:
    result = getattr(models, model_name).model_validate(FIXTURES["schema_drift"][operation])
    assert [warning.model_dump() for warning in result.parsing_warnings] == [
        {"code": "unknown_study_response", "path": path, "raw_value": "FUTURE_RESPONSE"}
    ]
    assert result.model_dump(exclude_unset=True) == FIXTURES["schema_drift"][operation]


@pytest.mark.parametrize(
    "value", ["FAMILIAR", "VAGUE", "FORGET", "WELL_FAMILIAR", "CANCEL_WELL_FAMILIAR"]
)
def test_known_response_values_need_no_warning(value: str) -> None:
    payload = copy.deepcopy(FIXTURES["complete"]["get_today_items"])
    payload["today_items"][0]["first_response"] = value
    assert models.TodayItemsResponse.model_validate(payload).parsing_warnings == []


def test_unfinished_first_response_is_optional_but_identifier_is_required() -> None:
    result = models.TodayItemsResponse.model_validate(FIXTURES["unfinished"])
    assert result.today_items[0].first_response is None
    assert result.parsing_warnings == []
    with pytest.raises(ValidationError):
        models.TodayItemsResponse.model_validate(FIXTURES["missing_identifier"])


@pytest.mark.parametrize(
    "name,payload",
    [
        ("StudyProgress", FIXTURES["complete"]["get_progress"]["progress"]),
        ("StudyTodayItem", FIXTURES["complete"]["get_today_items"]["today_items"][0]),
        ("StudyRecord", FIXTURES["complete"]["query_records"]["records"][0]),
    ],
)
def test_required_fields_and_nonnullable_schema(name: str, payload: dict[str, Any]) -> None:
    model = getattr(models, name)
    schema = SPEC["components"]["schemas"][name]
    for field in schema["required"]:
        invalid = dict(payload)
        del invalid[field]
        with pytest.raises(ValidationError):
            model.model_validate(invalid)
    for field in schema["properties"]:
        with pytest.raises(ValidationError):
            model.model_validate(dict(payload, **{field: None}))
    assert model.model_validate(dict(payload, future_optional=None)).model_extra == {
        "future_optional": None
    }
    for field, wrong_value in [("voc_id", 1), ("study_count", "2"), ("tags", "UNKNOWN")]:
        if field in payload:
            with pytest.raises(ValidationError):
                model.model_validate(dict(payload, **{field: wrong_value}))


@pytest.mark.parametrize(
    "request_model,field",
    [
        (TodayItemsRequest, "limit"),
        (StudyRecordsRequest, "limit"),
        (TodayItemsRequest, "voc_ids"),
        (TodayItemsRequest, "spellings"),
        (StudyRecordsRequest, "spellings"),
    ],
)
def test_query_records_enforces_one_thousand_item_limit(request_model: Any, field: str) -> None:
    request_model(**{field: 1000 if field == "limit" else ["synthetic"] * 1000})
    with pytest.raises(ValidationError):
        request_model(**{field: 1001 if field == "limit" else ["synthetic"] * 1001})
    request_model(**{field: -1 if field == "limit" else []})


def test_request_types_optional_fields_and_pinned_exclusions() -> None:
    assert TodayItemsRequest().model_dump(exclude_none=True) == {}
    assert StudyRecordsRequest(next_study_date=StudyDateRange()).model_dump(exclude_none=True) == {
        "next_study_date": {}
    }
    assert len(StudyRecordsRequest(voc_ids=["synthetic"] * 1001).voc_ids) == 1001
    for request in (TodayItemsRequest, StudyRecordsRequest):
        with pytest.raises(ValidationError):
            request(voc_ids=["a"], spellings=["apple"])
        for values in ({"limit": "1"}, {"voc_ids": [1]}, {"invented": True}):
            with pytest.raises(ValidationError):
                request(**values)
    for values in (
        {"tags": ["STICKING"]},
        {"tags": "WELL_FAMILIAR"},
        {"as_count": 1},
        {"next_study_date": {"start": 1}},
    ):
        with pytest.raises(ValidationError):
            StudyRecordsRequest(**values)


async def test_count_only_and_empty_body_preserve_upstream_meaning() -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"records": [], "count": 1200})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = StudyClient(
            MaimemoTransport(
                SecretStr("synthetic-token"), SecretStr("key"), NoWaitLimiter(), client=http
            )
        )
        result = await client.query_records(StudyRecordsRequest(as_count=True))
        assert result.count == 1200 and result.records == []
        assert result.parsing_warnings == []
        await client.query_records(StudyRecordsRequest())
    assert requests == [{"as_count": True}, {}]
