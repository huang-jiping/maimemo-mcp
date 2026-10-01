"""Observed learning facts; nullable upstream fields preserve unknown states."""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from maimemo_mcp.storage.base import Base, ObservationTimes, UUIDPrimaryKey


class Vocabulary(UUIDPrimaryKey, Base):
    __tablename__ = "vocabulary"
    __table_args__ = (
        CheckConstraint("maimemo_id > 0", name="maimemo_id"),
        CheckConstraint(
            "length(normalized_spelling) > 0 AND length(spelling) > 0", name="spelling"
        ),
        CheckConstraint("last_seen_at >= first_seen_at", name="time_order"),
    )

    maimemo_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    normalized_spelling: Mapped[str] = mapped_column(index=True)
    spelling: Mapped[str]
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DailyProgress(UUIDPrimaryKey, ObservationTimes, Base):
    __tablename__ = "daily_progress"
    __table_args__ = (
        CheckConstraint(
            "completed_count >= 0 AND total_count >= 0 "
            "AND total_count >= completed_count AND study_seconds >= 0",
            name="counts",
        ),
        CheckConstraint(
            "completeness IN ('complete', 'partial', 'stale', 'unavailable')",
            name="completeness",
        ),
        CheckConstraint("last_observed_at >= first_observed_at", name="time_order"),
    )

    study_date: Mapped[date] = mapped_column(unique=True)
    completed_count: Mapped[int | None]
    total_count: Mapped[int | None]
    study_seconds: Mapped[int | None]
    completeness: Mapped[str]


class DailyWordObservation(UUIDPrimaryKey, ObservationTimes, Base):
    __tablename__ = "daily_word_observation"
    __table_args__ = (
        UniqueConstraint("study_date", "vocabulary_id", name="uq_daily_word_observation_day_word"),
        CheckConstraint("first_feedback IS NULL OR length(first_feedback) > 0", name="feedback"),
        CheckConstraint("last_observed_at >= first_observed_at", name="time_order"),
        Index("ix_daily_word_observation_word_day", "vocabulary_id", "study_date"),
        Index("ix_daily_word_observation_source_snapshot_id", "source_snapshot_id"),
    )

    study_date: Mapped[date]
    vocabulary_id: Mapped[UUID] = mapped_column(ForeignKey("vocabulary.id"))
    first_feedback: Mapped[str | None]
    is_new: Mapped[bool | None]
    is_complete: Mapped[bool | None]
    source_snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("api_snapshot.id"))


class StudyRecordSnapshot(UUIDPrimaryKey, Base):
    __tablename__ = "study_record_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "vocabulary_id", "source_snapshot_id", name="uq_study_record_snapshot_source"
        ),
        CheckConstraint("study_count IS NULL OR study_count >= 0", name="study_count"),
        CheckConstraint("last_feedback IS NULL OR length(last_feedback) > 0", name="feedback"),
        CheckConstraint("jsonb_typeof(tags) = 'array'", name="tags"),
        Index("ix_study_record_snapshot_word_observed", "vocabulary_id", "observed_at"),
        Index("ix_study_record_snapshot_next_study_at", "next_study_at"),
        Index("ix_study_record_snapshot_source_snapshot_id", "source_snapshot_id"),
    )

    vocabulary_id: Mapped[UUID] = mapped_column(ForeignKey("vocabulary.id"))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    added_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_studied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_studied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_study_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_feedback: Mapped[str | None]
    study_count: Mapped[int | None]
    tags: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True))
    source_snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("api_snapshot.id"))
