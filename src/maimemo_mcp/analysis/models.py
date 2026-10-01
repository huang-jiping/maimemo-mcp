"""Explicit evidence inputs and versioned analysis outputs, independent of SQL."""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


@dataclass(frozen=True)
class DailyEvidence:
    study_date: date
    observed_at: datetime
    feedback: str | None
    is_complete: bool | None
    is_new: bool | None
    feedback_at: datetime | None = None


@dataclass(frozen=True)
class RecentResponse:
    feedback: str
    responded_at: datetime
    observed_at: datetime


@dataclass(frozen=True)
class WeaknessEvidence:
    vocabulary_id: UUID
    spelling: str
    observations: tuple[DailyEvidence, ...] = ()
    record_observed_at: datetime | None = None
    last_feedback: str | None = None
    last_studied_at: datetime | None = None
    next_study_at: datetime | None = None
    added_at: datetime | None = None
    study_count: int | None = None
    tags: tuple[str, ...] | None = None
    quality: str = "unavailable"
    latest_snapshot_id: UUID | None = None
    recent_responses: tuple[RecentResponse, ...] = ()


@dataclass(frozen=True)
class WeaknessFactor:
    value: float
    weight: int


@dataclass(frozen=True)
class WeaknessResult:
    vocabulary_id: UUID
    spelling: str
    computed_at: datetime
    algorithm_version: str
    score: float
    risk_level: Literal["low", "medium", "high"]
    confidence: float
    factors: dict[str, WeaknessFactor]
    reason_codes: tuple[str, ...]
    evidence_from: datetime | None
    evidence_through: datetime | None
    latest_snapshot_id: UUID | None
    is_new: bool | None
    reasons: tuple[str, ...] = field(default=())


class WeakWordQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: AwareDatetime
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    min_score: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    limit: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="after")
    def ordered_range(self) -> "WeakWordQuery":
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError("Evidence start must not be after end")
        return self
