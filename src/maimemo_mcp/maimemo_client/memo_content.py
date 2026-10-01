"""Seven pinned read operations for memo content and vocabulary."""

from typing import Self
from urllib.parse import quote

from pydantic import Field, TypeAdapter, model_validator

from maimemo_mcp.maimemo_client.markji import RequestModel
from maimemo_mcp.maimemo_client.models import (
    GetNotepadResponse,
    InterpretationsResponse,
    ListNotepadsResponse,
    NotesResponse,
    PhrasesResponse,
    QueryVocabularyResponse,
    VocabularyResponse,
)
from maimemo_mcp.maimemo_client.transport import MaimemoTransport

_STRING_PARAMETER = TypeAdapter(str)


class ListNotepadsRequest(RequestModel):
    limit: int | None = None
    offset: int | None = None
    ids: list[str] | None = None


class QueryVocabularyRequest(RequestModel):
    spellings: list[str] | None = Field(default=None, max_length=1000)
    ids: list[str] | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def exclusive_conditions(self) -> Self:
        if self.spellings and self.ids:
            raise ValueError("Vocabulary query conditions are mutually exclusive")
        return self


class MemoContentClient:
    def __init__(self, transport: MaimemoTransport) -> None:
        self._transport = transport

    async def get_interpretations(self, voc_id: str) -> InterpretationsResponse:
        voc_id = _STRING_PARAMETER.validate_python(voc_id, strict=True)
        return await self._transport.request(
            "GET",
            "/api/v1/memo/interpretations",
            params={"voc_id": voc_id},
            response_type=InterpretationsResponse,
        )

    async def get_notes(self, voc_id: str) -> NotesResponse:
        voc_id = _STRING_PARAMETER.validate_python(voc_id, strict=True)
        return await self._transport.request(
            "GET", "/api/v1/memo/notes", params={"voc_id": voc_id}, response_type=NotesResponse
        )

    async def list_notepads(self, request: ListNotepadsRequest) -> ListNotepadsResponse:
        return await self._transport.request(
            "GET",
            "/api/v1/memo/notepads",
            params=request.model_dump(exclude_none=True),
            response_type=ListNotepadsResponse,
        )

    async def get_notepad(self, notepad_id: str) -> GetNotepadResponse:
        notepad_id = _STRING_PARAMETER.validate_python(notepad_id, strict=True)
        return await self._transport.request(
            "GET",
            f"/api/v1/memo/notepads/{quote(notepad_id, safe='')}",
            response_type=GetNotepadResponse,
        )

    async def get_phrases(self, voc_id: str) -> PhrasesResponse:
        voc_id = _STRING_PARAMETER.validate_python(voc_id, strict=True)
        return await self._transport.request(
            "GET", "/api/v1/memo/phrases", params={"voc_id": voc_id}, response_type=PhrasesResponse
        )

    async def get_vocabulary(self, voc_id: str) -> VocabularyResponse:
        """The approved signature's voc_id aliases the pinned spelling parameter."""
        voc_id = _STRING_PARAMETER.validate_python(voc_id, strict=True)
        return await self._transport.request(
            "GET",
            "/api/v1/memo/vocabulary",
            params={"spelling": voc_id},
            response_type=VocabularyResponse,
        )

    async def query_vocabulary(self, request: QueryVocabularyRequest) -> QueryVocabularyResponse:
        return await self._transport.request(
            "POST",
            "/api/v1/memo/vocabulary/query",
            json=request.model_dump(exclude_none=True),
            response_type=QueryVocabularyResponse,
        )
