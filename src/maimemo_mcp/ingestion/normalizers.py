"""Normalize observed facts without inferring past events or unknown responses."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from maimemo_mcp.maimemo_client.models import (
    StudyProgressResponse,
    StudyRecordsResponse,
    TodayItemsResponse,
)
from maimemo_mcp.time import learning_date

SHANGHAI = ZoneInfo("Asia/Shanghai")


def utc_instant(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An explicit timezone is required")
    return value.astimezone(UTC)


def _date(value: str | None) -> datetime | None:
    return None if value is None else utc_instant(datetime.fromisoformat(value))


@dataclass(frozen=True)
class DailyProgressInput:
    study_date: date
    completed_count: int
    total_count: int
    study_seconds: int
    observed_at: datetime


@dataclass(frozen=True)
class DailyWordObservationInput:
    study_date: date
    maimemo_id: str
    spelling: str
    first_feedback: str | None
    is_new: bool
    is_complete: bool
    observed_at: datetime


@dataclass(frozen=True)
class StudyRecordSnapshotInput:
    maimemo_id: str
    spelling: str
    observed_at: datetime
    added_at: datetime | None
    first_studied_at: datetime | None
    last_studied_at: datetime | None
    next_study_at: datetime | None
    last_feedback: str | None
    study_count: int
    tags: list[str]


def normalize_daily_progress(
    response: StudyProgressResponse, observed_at: datetime
) -> DailyProgressInput:
    at = utc_instant(observed_at)
    progress = response.progress
    if not 0 <= progress.finished <= progress.total or progress.study_time < 0:
        raise ValueError("Invalid progress counts")
    return DailyProgressInput(
        learning_date(at, SHANGHAI),
        progress.finished,
        progress.total,
        progress.study_time // 1000,
        at,
    )


def normalize_today_items(
    response: TodayItemsResponse, observed_at: datetime
) -> list[DailyWordObservationInput]:
    at = utc_instant(observed_at)
    return [
        DailyWordObservationInput(
            learning_date(at, SHANGHAI),
            row.voc_id,
            row.voc_spelling,
            row.first_response,
            row.is_new,
            row.is_finished,
            at,
        )
        for row in response.today_items
    ]


def normalize_study_records(
    response: StudyRecordsResponse, observed_at: datetime
) -> list[StudyRecordSnapshotInput]:
    at = utc_instant(observed_at)
    return [
        StudyRecordSnapshotInput(
            row.voc_id,
            row.voc_spelling,
            at,
            _date(row.add_date),
            _date(row.first_study_date),
            _date(row.last_study_date),
            _date(row.next_study_date),
            row.last_response,
            row.study_count,
            [row.tags],
        )
        for row in response.records
    ]
