"""Public tool results with explicit UTC and stable, non-sensitive metadata codes."""

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator


class Completeness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


# Metadata contains caller-selected identifiers, never exception text, URLs or credentials.
# Do not put upstream payloads or secret values into these fields.
MetadataCode = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z][A-Za-z0-9_.:-]*$")]


class ToolMeta(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    source: list[MetadataCode]
    fetched_at: AwareDatetime
    data_through: date | None = None
    completeness: Completeness
    warnings: list[MetadataCode] = Field(default_factory=list)

    @field_validator("fetched_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class ToolEnvelope[T](BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    data: T
    meta: ToolMeta
