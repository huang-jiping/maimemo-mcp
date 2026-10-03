"""Append-only feedback identities and retraction references."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from maimemo.storage.base import Base, UUIDPrimaryKey


class LearningFeedbackEvent(UUIDPrimaryKey, Base):
    __tablename__ = "learning_feedback_event"
    __table_args__ = (
        CheckConstraint("event_type IN ('CONFUSION', 'RETRACTION')", name="event_type"),
        CheckConstraint(
            "evidence_type IN ('USER_CONFIRMED', 'QUIZ_OBSERVED')", name="evidence_type"
        ),
        CheckConstraint("direction IN ('A_TO_B', 'B_TO_A', 'BIDIRECTIONAL')", name="direction"),
        CheckConstraint("word_a_status IN ('RESOLVED', 'UNRESOLVED')", name="word_a_status"),
        CheckConstraint("word_b_status IN ('RESOLVED', 'UNRESOLVED')", name="word_b_status"),
        CheckConstraint("length(idempotency_key) > 0", name="idempotency_key"),
        CheckConstraint(
            "(event_type = 'RETRACTION' AND retracted_event_id IS NOT NULL "
            "AND retracted_event_id != id) "
            "OR (event_type = 'CONFUSION' AND retracted_event_id IS NULL "
            "AND relation_type IS NOT NULL AND length(relation_type) > 0 "
            "AND direction IS NOT NULL AND evidence_type IS NOT NULL "
            "AND source_agent IS NOT NULL AND length(source_agent) > 0 "
            "AND ((word_a_status = 'RESOLVED' AND word_a_id IS NOT NULL) "
            "OR (word_a_status = 'UNRESOLVED' AND word_a_id IS NULL "
            "AND word_a_spelling IS NOT NULL AND length(word_a_spelling) > 0)) "
            "AND ((word_b_status = 'RESOLVED' AND word_b_id IS NOT NULL) "
            "OR (word_b_status = 'UNRESOLVED' AND word_b_id IS NULL "
            "AND word_b_spelling IS NOT NULL AND length(word_b_spelling) > 0)))",
            name="event_shape",
        ),
        Index("ix_learning_feedback_event_word_a_created", "word_a_id", "created_at"),
        Index("ix_learning_feedback_event_word_b_created", "word_b_id", "created_at"),
        Index("ix_learning_feedback_event_created_at", "created_at"),
        Index("ix_learning_feedback_event_retracted_event_id", "retracted_event_id"),
    )

    event_type: Mapped[str]
    word_a_id: Mapped[UUID | None] = mapped_column(ForeignKey("vocabulary.id"))
    word_b_id: Mapped[UUID | None] = mapped_column(ForeignKey("vocabulary.id"))
    word_a_spelling: Mapped[str | None]
    word_b_spelling: Mapped[str | None]
    word_a_status: Mapped[str] = mapped_column(server_default="UNRESOLVED")
    word_b_status: Mapped[str] = mapped_column(server_default="UNRESOLVED")
    relation_type: Mapped[str | None]
    direction: Mapped[str | None]
    evidence_type: Mapped[str | None]
    note: Mapped[str | None]
    source_agent: Mapped[str | None]
    session_reference: Mapped[str | None]
    idempotency_key: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    retracted_event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("learning_feedback_event.id")
    )
