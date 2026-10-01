"""Catch lossy payload hashing and fabricated/incorrectly dated learning facts."""

from datetime import UTC, date, datetime

import pytest

from maimemo_mcp.ingestion.hashing import stable_payload_hash
from maimemo_mcp.ingestion.normalizers import (
    normalize_daily_progress,
    normalize_study_records,
    normalize_today_items,
)
from maimemo_mcp.maimemo_client.models import (
    StudyProgressResponse,
    StudyRecordsResponse,
    TodayItemsResponse,
)

AT = datetime(2026, 10, 1, 16, 30, tzinfo=UTC)


def test_hash_ignores_key_order_but_includes_endpoint_request_and_response() -> None:
    original = stable_payload_hash("today", {"b": 2, "a": 1}, {"x": {"a": 1, "b": 2}})
    assert original == stable_payload_hash("today", {"a": 1, "b": 2}, {"x": {"b": 2, "a": 1}})
    assert original != stable_payload_hash("records", {"a": 1, "b": 2}, {"x": {"a": 1, "b": 2}})
    assert original != stable_payload_hash("today", {"a": 2, "b": 2}, {"x": {"a": 1, "b": 2}})
    assert original != stable_payload_hash("today", {"a": 1, "b": 2}, {"x": {"a": 2, "b": 2}})


def test_progress_uses_shanghai_day_and_converts_milliseconds_to_seconds() -> None:
    value = normalize_daily_progress(
        StudyProgressResponse.model_validate(
            {"progress": {"finished": 1, "total": 2, "study_time": 61001}}
        ),
        AT,
    )
    assert value.study_date == date(2026, 10, 2)
    assert value.study_seconds == 61
    assert value.observed_at == AT


def test_today_missing_feedback_remains_unknown_and_future_values_survive() -> None:
    payload = {
        "today_items": [
            {
                "voc_id": "abc",
                "voc_spelling": "Apple",
                "order": 1,
                "is_new": True,
                "is_finished": False,
                "future": {"x": None},
            },
            {
                "voc_id": "def",
                "voc_spelling": "pear",
                "order": 2,
                "is_new": False,
                "is_finished": True,
                "first_response": "FUTURE",
            },
        ]
    }
    response = TodayItemsResponse.model_validate(payload)
    values = normalize_today_items(response, AT)
    assert values[0].first_feedback is None
    assert values[0].study_date == date(2026, 10, 2)
    assert values[1].first_feedback == "FUTURE"
    assert response.model_dump(exclude_unset=True) == payload


def test_record_missing_dates_remain_unknown_and_instants_are_utc() -> None:
    response = StudyRecordsResponse.model_validate(
        {
            "records": [
                {
                    "voc_id": "abc",
                    "voc_spelling": "apple",
                    "add_date": "2026-10-02T00:00:00+08:00",
                    "study_count": 3,
                    "tags": "STICKING",
                    "last_response": "FUTURE",
                }
            ],
            "count": 0,
        }
    )
    row = normalize_study_records(response, AT)[0]
    assert row.added_at == datetime(2026, 10, 1, 16, tzinfo=UTC)
    assert row.first_studied_at is None
    assert row.next_study_at is None
    assert row.last_feedback == "FUTURE"
    assert row.tags == ["STICKING"]


def test_naive_timestamp_is_rejected() -> None:
    response = StudyProgressResponse.model_validate(
        {"progress": {"finished": 0, "total": 0, "study_time": 0}}
    )
    with pytest.raises(ValueError):
        normalize_daily_progress(response, datetime(2026, 10, 2))
