"""Shared rate windows hold only irreversible token fingerprints."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from maimemo_mcp.storage.base import Base, UUIDPrimaryKey


class ApiRateLimitWindow(UUIDPrimaryKey, Base):
    __tablename__ = "api_rate_limit_window"
    __table_args__ = (
        UniqueConstraint(
            "token_hash", "window_type", "window_start", name="uq_api_rate_limit_window_identity"
        ),
        CheckConstraint("length(token_hash) = 64", name="token_hash"),
        CheckConstraint("window_type IN ('10s', '60s', '5h')", name="window_type"),
        CheckConstraint("used_requests >= 0", name="used_requests"),
        Index("ix_api_rate_limit_window_window_start", "window_start"),
    )

    token_hash: Mapped[str]
    window_type: Mapped[str]
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_requests: Mapped[int] = mapped_column(server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
