"""Worker runs and immutable deduplicated API responses."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from maimemo_mcp.storage.base import Base, UUIDPrimaryKey


class IngestionRun(UUIDPrimaryKey, Base):
    __tablename__ = "ingestion_run"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'complete', 'partial', 'failed')", name="status"),
        CheckConstraint("request_count >= 0 AND result_count >= 0", name="counts"),
        CheckConstraint("finished_at IS NULL OR finished_at >= started_at", name="time_order"),
        Index("ix_ingestion_run_task_started", "task_type", "started_at"),
    )

    task_type: Mapped[str]
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str]
    request_count: Mapped[int] = mapped_column(server_default="0")
    result_count: Mapped[int] = mapped_column(server_default="0")
    error_category: Mapped[str | None]
    error_summary: Mapped[str | None]


class ApiSnapshot(UUIDPrimaryKey, Base):
    __tablename__ = "api_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "endpoint", "request_hash", "content_hash", name="uq_api_snapshot_content"
        ),
        CheckConstraint("length(endpoint) > 0", name="endpoint"),
        CheckConstraint("length(request_hash) > 0 AND length(content_hash) > 0", name="hashes"),
        Index("ix_api_snapshot_endpoint_fetched", "endpoint", "fetched_at"),
        Index("ix_api_snapshot_ingestion_run_id", "ingestion_run_id"),
    )

    endpoint: Mapped[str]
    request_hash: Mapped[str]
    content_hash: Mapped[str]
    raw_response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ingestion_run_id: Mapped[UUID] = mapped_column(ForeignKey("ingestion_run.id"))
