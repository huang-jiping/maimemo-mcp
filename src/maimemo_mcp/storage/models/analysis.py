"""Versioned, explainable analysis with traceable evidence."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from maimemo_mcp.storage.base import Base, UUIDPrimaryKey


class WeaknessScore(UUIDPrimaryKey, Base):
    __tablename__ = "weakness_score"
    __table_args__ = (
        UniqueConstraint(
            "vocabulary_id", "algorithm_version", "computed_at", name="uq_weakness_score_version"
        ),
        CheckConstraint("score >= 0 AND score <= 100", name="score"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence"),
        CheckConstraint("risk_level IN ('low', 'medium', 'high')", name="risk_level"),
        CheckConstraint("length(algorithm_version) > 0", name="algorithm_version"),
        CheckConstraint("jsonb_typeof(factors) = 'object'", name="factors"),
        CheckConstraint("evidence_through >= evidence_from", name="time_order"),
        Index("ix_weakness_score_version_score", "algorithm_version", "score"),
        Index("ix_weakness_score_computed_at", "computed_at"),
        Index("ix_weakness_score_latest_snapshot_id", "latest_snapshot_id"),
    )

    vocabulary_id: Mapped[UUID] = mapped_column(ForeignKey("vocabulary.id"))
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    algorithm_version: Mapped[str]
    score: Mapped[float]
    risk_level: Mapped[str]
    confidence: Mapped[float]
    factors: Mapped[dict[str, Any]] = mapped_column(JSONB)
    evidence_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence_through: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latest_snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("api_snapshot.id"))
