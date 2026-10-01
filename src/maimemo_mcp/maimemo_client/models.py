"""Pinned response schemas, preserving unknown optional upstream fields."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    @field_validator("*", mode="before")
    @classmethod
    def reject_explicit_null(cls, value: Any) -> Any:
        """Pinned fields may be omitted, but none of them declares a nullable type.

        Field validators run on supplied known fields, leaving omitted defaults and
        unknown upstream extensions intact.
        """
        if value is None:
            raise ValueError("Declared response fields cannot be null")
        return value


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


class Interpretation(ResponseModel):
    id: str
    interpretation: str
    tags: list[str]
    status: Literal["PUBLISHED", "UNPUBLISHED", "DELETED"]
    created_time: str
    updated_time: str


class Note(ResponseModel):
    id: str
    note_type: str
    note: str
    status: Literal["PUBLISHED", "DELETED"]
    created_time: str
    updated_time: str


class BriefNotepad(ResponseModel):
    id: str
    type: Literal["FAVORITE", "NOTEPAD"]
    creator: int
    status: Literal["PUBLISHED", "UNPUBLISHED", "DELETED"]
    title: str
    brief: str
    tags: list[str]
    created_time: str
    updated_time: str


class NotepadParsedData(ResponseModel):
    chapter: str
    word: str | None = None


class NotepadParsedItem(ResponseModel):
    type: Literal["CHAPTER", "WORD"]
    data: NotepadParsedData


class Notepad(BriefNotepad):
    content: str
    list: list[NotepadParsedItem]


class PhraseHighlightRange(ResponseModel):
    start: int
    end: int


class Phrase(ResponseModel):
    id: str
    phrase: str
    interpretation: str
    tags: list[str]
    highlight: list[PhraseHighlightRange]
    status: Literal["PUBLISHED", "DELETED"]
    created_time: str
    updated_time: str
    origin: str


class Vocabulary(ResponseModel):
    id: str
    spelling: str


class InterpretationsResponse(ResponseModel):
    interpretations: list[Interpretation]


class NotesResponse(ResponseModel):
    notes: list[Note]


class ListNotepadsResponse(ResponseModel):
    notepads: list[BriefNotepad]


class GetNotepadResponse(ResponseModel):
    notepad: Notepad


class PhrasesResponse(ResponseModel):
    phrases: list[Phrase]


class VocabularyResponse(ResponseModel):
    voc: Vocabulary


class QueryVocabularyResponse(ResponseModel):
    voc: list[Vocabulary]
