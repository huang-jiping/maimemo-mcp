"""Import all mapped tables for Alembic metadata discovery."""

from maimemo.storage.base import SchemaMetadata
from maimemo.storage.models.analysis import WeaknessScore
from maimemo.storage.models.feedback import LearningFeedbackEvent
from maimemo.storage.models.ingestion import ApiSnapshot, FailedApiSnapshot, IngestionRun
from maimemo.storage.models.learning import (
    DailyProgress,
    DailyWordObservation,
    StudyRecordSnapshot,
    Vocabulary,
)
from maimemo.storage.models.rate_limit import ApiRateLimitWindow

__all__ = [
    "ApiRateLimitWindow",
    "ApiSnapshot",
    "FailedApiSnapshot",
    "DailyProgress",
    "DailyWordObservation",
    "IngestionRun",
    "LearningFeedbackEvent",
    "SchemaMetadata",
    "StudyRecordSnapshot",
    "Vocabulary",
    "WeaknessScore",
]
