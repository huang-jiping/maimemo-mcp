"""Seven read operations from the pinned Markji OpenAPI snapshot."""

from pydantic import BaseModel, ConfigDict

from maimemo.api_client.models import (
    GetCardResponse,
    GetChapterResponse,
    GetDeckResponse,
    ListChaptersResponse,
    ListDecksResponse,
    ListFoldersResponse,
    QueryFilesResponse,
)
from maimemo.api_client.transport import MaimemoTransport, encode_path_id


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ListFoldersRequest(RequestModel):
    """The pinned operation has no request parameters."""


class ListDecksRequest(RequestModel):
    offset: int | None = None
    limit: int | None = None
    folder_id: str | None = None


class QueryFilesRequest(RequestModel):
    ids: list[str]
    expires: int | None = None


class MarkjiClient:
    def __init__(self, transport: MaimemoTransport) -> None:
        self._transport = transport

    async def list_folders(self, request: ListFoldersRequest) -> ListFoldersResponse:
        return await self._transport.request(
            "GET", "/api/v1/markji/decks/folders", response_type=ListFoldersResponse
        )

    async def list_decks(self, request: ListDecksRequest) -> ListDecksResponse:
        return await self._transport.request(
            "GET",
            "/api/v1/markji/decks",
            params=request.model_dump(exclude_none=True),
            response_type=ListDecksResponse,
        )

    async def get_deck(self, deck: str) -> GetDeckResponse:
        return await self._transport.request(
            "GET", f"/api/v1/markji/decks/{encode_path_id(deck)}", response_type=GetDeckResponse
        )

    async def list_chapters(self, deck: str) -> ListChaptersResponse:
        return await self._transport.request(
            "GET",
            f"/api/v1/markji/decks/{encode_path_id(deck)}/chapters",
            response_type=ListChaptersResponse,
        )

    async def get_chapter(self, deck: str, chapter: str) -> GetChapterResponse:
        return await self._transport.request(
            "GET",
            f"/api/v1/markji/decks/{encode_path_id(deck)}/chapters/{encode_path_id(chapter)}",
            response_type=GetChapterResponse,
        )

    async def get_card(self, deck: str, card: str) -> GetCardResponse:
        return await self._transport.request(
            "GET",
            f"/api/v1/markji/decks/{encode_path_id(deck)}/cards/{encode_path_id(card)}",
            response_type=GetCardResponse,
        )

    async def query_files(self, request: QueryFilesRequest) -> QueryFilesResponse:
        return await self._transport.request(
            "POST",
            "/api/v1/markji/files/query",
            json=request.model_dump(exclude_none=True),
            response_type=QueryFilesResponse,
        )
