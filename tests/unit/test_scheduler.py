"""Scheduling boundaries catch local-time drift and duplicate emitted slots."""

from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from maimemo_mcp.config import Settings
from maimemo_mcp.ingestion.scheduler import Schedule

SHANGHAI = ZoneInfo("Asia/Shanghai")


def settings(
    *,
    today_interval_minutes: int | None = None,
    records_interval_minutes: int | None = None,
) -> Settings:
    values: dict[str, object] = {
        "database_url": "postgresql+psycopg://unused",
        "token_file": Path("unused"),
        "token_fingerprint_key_file": Path("unused"),
    }
    if today_interval_minutes is not None:
        values["today_interval_minutes"] = today_interval_minutes
    if records_interval_minutes is not None:
        values["records_interval_minutes"] = records_interval_minutes
    return Settings.model_validate(values)


def at(hour: int, minute: int = 0, *, day: int = 2) -> datetime:
    return datetime(2030, 10, day, hour, minute, tzinfo=SHANGHAI)


def test_startup_catches_up_current_slots_once() -> None:
    schedule = Schedule(settings())
    jobs = schedule.next_runs(at(10, 17))
    assert [(job.task_type, job.scheduled_at) for job in jobs] == [
        ("daily_summary", at(0)),
        ("today", at(10)),
        ("records", at(10)),
    ]
    assert jobs[0].study_date == date(2030, 10, 1)
    assert schedule.next_runs(at(10, 18)) == []


def test_default_intervals_emit_today_at_30_minutes_and_records_at_two_hours() -> None:
    schedule = Schedule(settings())
    schedule.next_runs(at(10, 17))
    assert [job.task_type for job in schedule.next_runs(at(10, 29))] == []
    assert [(job.task_type, job.scheduled_at) for job in schedule.next_runs(at(10, 30))] == [
        ("today", at(10, 30)),
    ]
    assert [job.task_type for job in schedule.next_runs(at(11, 59))] == ["today"]
    assert [job.task_type for job in schedule.next_runs(at(12))] == ["today", "records"]


def test_midnight_summarizes_just_finished_shanghai_day() -> None:
    schedule = Schedule(settings())
    schedule.next_runs(at(23, 59))
    jobs = schedule.next_runs(at(0, day=3).astimezone(UTC))
    assert [job.task_type for job in jobs] == ["daily_summary", "today", "records"]
    assert jobs[0].study_date == date(2030, 10, 2)
    assert all(job.scheduled_at == at(0, day=3) for job in jobs)
    assert all(str(job.scheduled_at.tzinfo) == "Asia/Shanghai" for job in jobs)


def test_configured_intervals_change_observable_boundaries() -> None:
    schedule = Schedule(settings(today_interval_minutes=15, records_interval_minutes=60))
    schedule.next_runs(at(10))
    assert [job.task_type for job in schedule.next_runs(at(10, 15))] == ["today"]
    assert [job.task_type for job in schedule.next_runs(at(11))] == ["today", "records"]


def test_naive_clock_is_rejected_before_emitting_jobs() -> None:
    schedule = Schedule(settings())
    with pytest.raises(ValueError, match="timezone"):
        schedule.next_runs(datetime(2030, 10, 2, 10))
    assert len(schedule.next_runs(at(10))) == 3


def test_configured_multi_day_interval_is_not_reset_each_midnight() -> None:
    schedule = Schedule(settings(records_interval_minutes=2880))
    first = datetime(1970, 1, 1, 10, tzinfo=SHANGHAI)
    assert [job.task_type for job in schedule.next_runs(first)] == [
        "daily_summary",
        "today",
        "records",
    ]
    assert [job.task_type for job in schedule.next_runs(datetime(1970, 1, 2, tzinfo=SHANGHAI))] == [
        "daily_summary",
        "today",
    ]
    assert [job.task_type for job in schedule.next_runs(datetime(1970, 1, 3, tzinfo=SHANGHAI))] == [
        "daily_summary",
        "today",
        "records",
    ]
