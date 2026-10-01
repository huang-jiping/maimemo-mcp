"""Import all mapped tables for Alembic metadata discovery."""

from maimemo_mcp.storage.base import SchemaMetadata
from maimemo_mcp.storage.models.analysis import WeaknessScore
from maimemo_mcp.storage.models.feedback import LearningFeedbackEvent
from maimemo_mcp.storage.models.ingestion import ApiSnapshot, IngestionRun
from maimemo_mcp.storage.models.learning import (
    DailyProgress,
    DailyWordObservation,
    StudyRecordSnapshot,
    Vocabulary,
)
from maimemo_mcp.storage.models.rate_limit import ApiRateLimitWindow

__all__ = [
    "ApiRateLimitWindow",
    "ApiSnapshot",
    "DailyProgress",
    "DailyWordObservation",
    "IngestionRun",
    "LearningFeedbackEvent",
    "SchemaMetadata",
    "StudyRecordSnapshot",
    "Vocabulary",
    "WeaknessScore",
]
