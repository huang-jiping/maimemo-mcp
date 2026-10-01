"""Validated feedback inputs; matching normalization never rewrites original text."""

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator


def normalize_spelling(spelling: str) -> str:
    return spelling.strip().casefold()


class FeedbackRelationType(StrEnum):
    FORM_SIMILAR = "FORM_SIMILAR"
    SOUND_SIMILAR = "SOUND_SIMILAR"
    MEANING_OVERLAP = "MEANING_OVERLAP"
    USAGE_CONTRAST = "USAGE_CONTRAST"


class FeedbackDirection(StrEnum):
    A_TO_B = "A_TO_B"
    B_TO_A = "B_TO_A"
    BIDIRECTIONAL = "BIDIRECTIONAL"


class EvidenceType(StrEnum):
    USER_CONFIRMED = "USER_CONFIRMED"
    QUIZ_OBSERVED = "QUIZ_OBSERVED"


class FeedbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FeedbackWrite(FeedbackInput):
    idempotency_key: str = Field(min_length=1, max_length=200)
    source_agent: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=4000)
    session_reference: str | None = Field(default=None, max_length=500)

    @field_validator("idempotency_key", "source_agent")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Value must not be blank")
        return value


class RecordFeedbackCommand(FeedbackWrite):
    word_a_spelling: str = Field(min_length=1, max_length=500)
    word_b_spelling: str = Field(min_length=1, max_length=500)
    relation_type: FeedbackRelationType
    direction: FeedbackDirection
    evidence_type: EvidenceType
    explicit_confirmation: StrictBool = False

    @model_validator(mode="after")
    def explicit_distinct_evidence(self) -> Self:
        a, b = normalize_spelling(self.word_a_spelling), normalize_spelling(self.word_b_spelling)
        if not a or not b or a == b:
            raise ValueError("Feedback requires two nonblank, distinct spellings")
        if self.evidence_type == EvidenceType.USER_CONFIRMED and not self.explicit_confirmation:
            raise ValueError("USER_CONFIRMED requires explicit_confirmation=true")
        if self.evidence_type == EvidenceType.QUIZ_OBSERVED and self.explicit_confirmation:
            raise ValueError("QUIZ_OBSERVED cannot claim explicit user confirmation")
        return self


class RetractFeedbackCommand(FeedbackWrite):
    event_id: UUID


class FeedbackQuery(FeedbackInput):
    word_spelling: str | None = Field(default=None, min_length=1, max_length=500)
    word_id: UUID | None = None
    direction: FeedbackDirection | None = None
    relation_type: FeedbackRelationType | None = None
    evidence_type: EvidenceType | None = None
    limit: int = Field(default=100, ge=1, le=1000)

    @field_validator("word_spelling")
    @classmethod
    def nonblank_word(cls, value: str | None) -> str | None:
        if value is not None and not normalize_spelling(value):
            raise ValueError("Word filter must not be blank")
        return value


class FeedbackEventView(FeedbackInput):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: UUID
    event_type: Literal["CONFUSION", "RETRACTION"]
    word_a_id: UUID | None
    word_b_id: UUID | None
    word_a_spelling: str | None
    word_b_spelling: str | None
    word_a_status: Literal["RESOLVED", "UNRESOLVED"]
    word_b_status: Literal["RESOLVED", "UNRESOLVED"]
    relation_type: FeedbackRelationType | None
    direction: FeedbackDirection | None
    evidence_type: EvidenceType | None
    note: str | None
    source_agent: str | None
    session_reference: str | None
    idempotency_key: str
    created_at: datetime
    retracted_event_id: UUID | None
