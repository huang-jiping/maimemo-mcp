"""Explicit Shanghai calendar slots, with bounded startup catch-up."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

from maimemo.config import AnalysisIntervals
from maimemo.ingestion.locks import advisory_key as advisory_key
from maimemo.ingestion.normalizers import SHANGHAI, utc_instant


@dataclass(frozen=True)
class ScheduledJob:
    task_type: Literal["today", "records", "daily_summary"]
    scheduled_at: datetime
    study_date: date | None = None

    def __post_init__(self) -> None:
        utc_instant(self.scheduled_at)
        if self.task_type == "daily_summary" and self.study_date is None:
            raise ValueError("Daily summary requires its observed study date")

    @property
    def identity(self) -> str:
        if self.task_type == "daily_summary":
            return f"daily_summary:{self.study_date}"
        return self.task_type


class Schedule:
    def __init__(self, intervals: AnalysisIntervals) -> None:
        self.today_interval = intervals.today_interval
        self.records_interval = intervals.records_interval
        self._last_emitted: dict[str, datetime] = {}

    def next_runs(self, now: datetime) -> list[ScheduledJob]:
        local = utc_instant(now).astimezone(SHANGHAI)
        midnight = datetime.combine(local.date(), time.min, SHANGHAI)
        jobs = [ScheduledJob("daily_summary", midnight, local.date() - timedelta(days=1))]
        streams: tuple[tuple[Literal["today", "records"], timedelta], ...] = (
            ("today", self.today_interval),
            ("records", self.records_interval),
        )
        anchor = datetime(1970, 1, 1, tzinfo=SHANGHAI)
        for task, interval in streams:
            # A fixed Shanghai midnight keeps configurable intervals continuous
            # across days, including intervals longer than one learning day.
            slot = anchor + ((local - anchor) // interval) * interval
            jobs.append(ScheduledJob(task, slot))
        due = []
        for job in jobs:
            last = self._last_emitted.get(job.task_type)
            if last is None or job.scheduled_at > last:
                due.append(job)
                self._last_emitted[job.task_type] = job.scheduled_at
        return due
