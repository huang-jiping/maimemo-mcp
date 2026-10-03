"""Three pinned study reads; initialization and history belong to higher layers."""

from typing import Literal, Self

from pydantic import Field, model_validator

from maimemo.api_client.markji import RequestModel
from maimemo.api_client.models import (
    StudyProgressResponse,
    StudyRecordsResponse,
    TodayItemsResponse,
)
from maimemo.api_client.transport import MaimemoTransport


class StudyDateRange(RequestModel):
    start: str | None = None
    end: str | None = None


class StudyQueryRequest(RequestModel):
    voc_ids: list[str] | None = None
    spellings: list[str] | None = Field(default=None, max_length=1000)
    limit: int | None = Field(default=None, le=1000)

    @model_validator(mode="after")
    def exclusive_word_conditions(self) -> Self:
        if self.voc_ids and self.spellings:
            raise ValueError("Study word query conditions are mutually exclusive")
        return self


class TodayItemsRequest(StudyQueryRequest):
    voc_ids: list[str] | None = Field(default=None, max_length=1000)
    is_finished: bool | None = None
    is_new: bool | None = None


class StudyRecordsRequest(StudyQueryRequest):
    next_study_date: StudyDateRange | None = None
    tags: Literal["STICKING"] | None = None
    as_count: bool | None = None


class StudyClient:
    def __init__(self, transport: MaimemoTransport) -> None:
        self._transport = transport

    async def get_progress(self) -> StudyProgressResponse:
        return await self._transport.request(
            "POST", "/api/v1/memo/study/get_study_progress", response_type=StudyProgressResponse
        )

    async def get_today_items(self, request: TodayItemsRequest) -> TodayItemsResponse:
        return await self._transport.request(
            "POST",
            "/api/v1/memo/study/get_today_items",
            json=request.model_dump(exclude_none=True),
            response_type=TodayItemsResponse,
        )

    async def query_records(self, request: StudyRecordsRequest) -> StudyRecordsResponse:
        return await self._transport.request(
            "POST",
            "/api/v1/memo/study/query_study_records",
            json=request.model_dump(exclude_none=True),
            response_type=StudyRecordsResponse,
        )
