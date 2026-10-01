"""Pinned response schemas, preserving unknown optional upstream fields."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)


class MarkjiRootDeck(ResponseModel):
    id: str
    parent_id: str | None = None
    source: Literal["SELF", "FORK"]
    status: Literal["NORMAL", "DELETED", "BLOCKED"]
    name: str
    description: str
    creator: str
    authors: list[str]
    revision: int
    is_private: bool
    card_count: int
    chapter_count: int
    created_time: str
    updated_time: str


class MarkjiDeck(MarkjiRootDeck):
    root_deck: MarkjiRootDeck | None = None


class MarkjiChapterset(ResponseModel):
    id: str
    deck_id: str
    revision: int
    chapter_ids: list[str]
    created_time: str
    updated_time: str


class MarkjiChapter(ResponseModel):
    id: str
    deck_id: str
    name: str
    revision: int
    card_ids: list[str]
    creator: str
    created_time: str
    updated_time: str


class MarkjiFile(ResponseModel):
    id: str
    url: str
    mime: str
    size: int
    info: dict[str, Any]
    expire_time: str


class MarkjiCard(ResponseModel):
    id: str
    status: Literal["NORMAL", "DELETED", "BLOCKED"]
    deck_id: str
    parent_id: str | None = None
    root_id: str | None = None
    revision: int
    content: str
    content_type: Literal["PLAIN"]
    files: list[MarkjiFile]
    creator: str
    source: Literal["SELF", "FORK"]
    grammar_version: int
    card_rids: list[str] | None = None
    created_time: str
    updated_time: str


class MarkjiFolderItem(ResponseModel):
    object_id: str
    object_class: Literal["FOLDER", "DECK"]
    order: int


class MarkjiFolder(ResponseModel):
    id: str
    items: list[MarkjiFolderItem]
    parent_id: str | None = None
    name: str


class ListFoldersResponse(ResponseModel):
    folders: list[MarkjiFolder]


class ListDecksResponse(ResponseModel):
    decks: list[MarkjiDeck]
    total: int


class GetDeckResponse(ResponseModel):
    deck: MarkjiDeck


class ListChaptersResponse(ResponseModel):
    chapterset: MarkjiChapterset | None = None
    chapters: list[MarkjiChapter]
    cards: list[MarkjiCard] | None = None


class GetChapterResponse(ResponseModel):
    chapterset: MarkjiChapterset | None = None
    chapter: MarkjiChapter | None = None
    cards: list[MarkjiCard] | None = None


class GetCardResponse(ResponseModel):
    card: MarkjiCard


class QueryFilesResponse(ResponseModel):
    files: list[MarkjiFile]
