"""Atomic Worker collection and bounded, count-first record windows."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maimemo_mcp.ingestion.normalizers import (
    SHANGHAI,
    normalize_daily_progress,
    normalize_study_records,
    normalize_today_items,
    utc_instant,
)
from maimemo_mcp.maimemo_client.models import (
    StudyProgressResponse,
    StudyRecordsResponse,
    TodayItemsResponse,
)
from maimemo_mcp.maimemo_client.study import (
    StudyDateRange,
    StudyRecordsRequest,
    TodayItemsRequest,
)
from maimemo_mcp.maimemo_client.transport import HttpAttemptObservation, observe_http_attempts
from maimemo_mcp.storage.models.ingestion import IngestionRun
from maimemo_mcp.storage.models.learning import DailyProgress
from maimemo_mcp.storage.repositories import StudyHistoryRepository

PROGRESS = "/api/v1/memo/study/get_study_progress"
TODAY = "/api/v1/memo/study/get_today_items"
RECORDS = "/api/v1/memo/study/query_study_records"


class StudyReads(Protocol):
    async def get_progress(self) -> StudyProgressResponse: ...
    async def get_today_items(self, request: TodayItemsRequest) -> TodayItemsResponse: ...
    async def query_records(self, request: StudyRecordsRequest) -> StudyRecordsResponse: ...


@dataclass(frozen=True)
class IngestionResult:
    run_id: UUID
    status: Literal["complete", "partial", "failed"]
    request_count: int
    result_count: int
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StudyRecordWindow:
    range_start: datetime
    range_end: datetime
    count: int
    saturated: bool = False

    def request(self, *, as_count: bool) -> StudyRecordsRequest:
        return StudyRecordsRequest(
            next_study_date=StudyDateRange(
                start=self.range_start.astimezone(SHANGHAI).isoformat(),
                end=self.range_end.astimezone(SHANGHAI).isoformat(),
            ),
            as_count=as_count,
            limit=1000,
        )


class StudyRecordWindowPlanner:
    """Split at Shanghai midnights, respecting inclusive microsecond-resolution bounds."""

    def __init__(self, client: StudyReads) -> None:
        self.client = client
        self.responses: list[tuple[StudyRecordsRequest, StudyRecordsResponse]] = []
        self.warnings: list[str] = []

    async def plan(self, range_start: datetime, range_end: datetime) -> list[StudyRecordWindow]:
        start, end = utc_instant(range_start), utc_instant(range_end)
        if start > end:
            raise ValueError("Record range start must precede end")
        self.responses = []
        self.warnings = []
        return await self._split(start, end)

    async def _split(self, start: datetime, end: datetime) -> list[StudyRecordWindow]:
        request = StudyRecordWindow(start, end, 0).request(as_count=True)
        response = await self.client.query_records(request)
        self.responses.append((request, response))
        count = response.count
        if count < 0:
            raise ValueError("Invalid record count")
        first_day, last_day = start.astimezone(SHANGHAI).date(), end.astimezone(SHANGHAI).date()
        if first_day == last_day:
            return [StudyRecordWindow(start, end, count, count >= 1000)]
        if count <= 1000:
            return [StudyRecordWindow(start, end, count)]
        days = (last_day - first_day).days
        split_day = first_day + timedelta(days=(days + 1) // 2)
        midpoint = datetime.combine(split_day, time.min, SHANGHAI).astimezone(UTC)
        left = await self._split(start, midpoint - timedelta(microseconds=1))
        right = await self._split(midpoint, end)
        if sum(window.count for window in left + right) != count:
            self.warnings.append("truncation: parent and child window counts disagree")
        return left + right


class StudyIngestionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: StudyReads,
        *,
        records_range_start: datetime = datetime(1970, 1, 1, tzinfo=UTC),
        records_range_end: datetime = datetime(2100, 1, 1, tzinfo=UTC),
    ) -> None:
        self.session_factory = session_factory
        self.client = client
        self.records_range_start = utc_instant(records_range_start)
        self.records_range_end = utc_instant(records_range_end)

    async def collect_today(self, observed_at: datetime) -> IngestionResult:
        return await self._collect("today", observed_at, self._today)

    async def collect_records(self, observed_at: datetime) -> IngestionResult:
        return await self._collect("records", observed_at, self._records)

    async def _collect(
        self,
        task: str,
        observed_at: datetime,
        action: Callable[
            [StudyHistoryRepository, IngestionRun, datetime], Awaitable[tuple[int, list[str]]]
        ],
    ) -> IngestionResult:
        with observe_http_attempts() as attempts:
            return await self._collect_observed(task, observed_at, action, attempts)

    async def _collect_observed(
        self,
        task: str,
        observed_at: datetime,
        action: Callable[
            [StudyHistoryRepository, IngestionRun, datetime], Awaitable[tuple[int, list[str]]]
        ],
        attempts: HttpAttemptObservation,
    ) -> IngestionResult:
        at = utc_instant(observed_at)
        run = IngestionRun(
            task_type=task, started_at=at, status="running", request_count=0, result_count=0
        )
        try:
            async with self.session_factory.begin() as session:
                # Serializes all formal streams so first committed baseline is deterministic.
                await session.execute(text("SELECT pg_advisory_xact_lock(724966203)"))
                session.add(run)
                await session.flush()
                result_count, warnings = await action(StudyHistoryRepository(session), run, at)
                run.request_count = attempts.count
                run.result_count = result_count
                run.status = "partial" if warnings else "complete"
                run.error_category = "incomplete" if warnings else None
                run.error_summary = "; ".join(warnings) if warnings else None
                run.finished_at = max(datetime.now(UTC), at)
                result = IngestionResult(
                    run.id,
                    "partial" if warnings else "complete",
                    run.request_count,
                    result_count,
                    warnings,
                )
            return result
        except Exception as exc:
            # No exception message is safe: it may contain token, SQL parameters or payload.
            category = "normalization" if isinstance(exc, ValueError) else "collection"
            failure = IngestionRun(
                task_type=task,
                started_at=at,
                finished_at=max(datetime.now(UTC), at),
                status="failed",
                request_count=attempts.count,
                result_count=0,
                error_category=category,
                error_summary=f"{category} failed; inspect sanitized operational diagnostics",
            )
            async with self.session_factory.begin() as session:
                session.add(failure)
                await session.flush()
                result = IngestionResult(
                    failure.id,
                    "failed",
                    failure.request_count,
                    0,
                    [failure.error_summary or "collection failed"],
                )
            return result

    async def _today(
        self,
        repo: StudyHistoryRepository,
        run: IngestionRun,
        at: datetime,
    ) -> tuple[int, list[str]]:
        progress_response = await self.client.get_progress()
        progress = normalize_daily_progress(progress_response, at)
        # Date context is part of the formal request identity; identical consecutive days
        # remain distinct observations even though these upstream requests have no date field.
        context: dict[str, Any] = {"learning_date": progress.study_date.isoformat()}
        await repo.snapshot(
            PROGRESS, context, progress_response.model_dump(exclude_unset=True), at, run.id
        )
        request = TodayItemsRequest(limit=1000)
        response = await self.client.get_today_items(request)
        snapshot = await repo.snapshot(
            TODAY,
            {**request.model_dump(exclude_none=True), **context},
            response.model_dump(exclude_unset=True),
            at,
            run.id,
        )
        items = normalize_today_items(response, at)
        warnings: list[str] = []
        if not items and progress.total_count == 0 and progress.completed_count == 0:
            warnings.append("INCOMPLETE: empty day may not be initialized in the app")
            existing = await repo.session.scalar(
                select(DailyProgress).where(DailyProgress.study_date == progress.study_date)
            )
            if existing is None or not any(
                (existing.completed_count, existing.total_count, existing.study_seconds)
            ):
                await repo.progress(progress, "partial")
        else:
            if len(items) >= 1000 or len(items) != progress.total_count:
                warnings.append(
                    "INCOMPLETE: today items count differs from progress or reaches limit"
                )
            if len({item.maimemo_id for item in items}) != len(items):
                warnings.append("INCOMPLETE: duplicate today item IDs")
            await repo.progress(progress, "partial" if warnings else "complete")
        await repo.today(items, snapshot.id)
        return len({row.maimemo_id for row in items}), warnings

    async def _records(
        self,
        repo: StudyHistoryRepository,
        run: IngestionRun,
        at: datetime,
    ) -> tuple[int, list[str]]:
        request = StudyRecordsRequest(as_count=True)
        total = await self.client.query_records(request)
        if total.count < 0:
            raise ValueError("Invalid total count")
        await repo.snapshot(
            RECORDS,
            request.model_dump(exclude_none=True),
            total.model_dump(exclude_unset=True),
            at,
            run.id,
        )

        planner = StudyRecordWindowPlanner(self.client)
        windows = await planner.plan(self.records_range_start, self.records_range_end)
        for count_request, count_response in planner.responses:
            await repo.snapshot(
                RECORDS,
                count_request.model_dump(exclude_none=True),
                count_response.model_dump(exclude_unset=True),
                at,
                run.id,
            )
        warnings = planner.warnings.copy()
        seen: set[str] = set()

        async def save_rows(row_request: StudyRecordsRequest) -> int:
            response = await self.client.query_records(row_request)
            snapshot = await repo.snapshot(
                RECORDS,
                row_request.model_dump(exclude_none=True),
                response.model_dump(exclude_unset=True),
                at,
                run.id,
            )
            rows = normalize_study_records(response, at)
            new_rows = []
            for row in rows:
                if row.next_study_at is None:
                    warnings.append("truncation: record has no queryable next study date")
                elif row_request.next_study_date is not None:
                    bounds = row_request.next_study_date
                    if (
                        bounds.start and row.next_study_at < datetime.fromisoformat(bounds.start)
                    ) or (bounds.end and row.next_study_at > datetime.fromisoformat(bounds.end)):
                        warnings.append("truncation: record lies outside its requested window")
                if row.maimemo_id in seen:
                    if row_request.next_study_date is not None:
                        warnings.append("truncation: duplicate IDs across record windows")
                    continue
                seen.add(row.maimemo_id)
                new_rows.append(row)
            await repo.records(new_rows, snapshot.id)
            return len(rows)

        for window in windows:
            if window.saturated:
                warnings.append(
                    "truncation: indivisible learning day reaches the 1000 record limit"
                )
            if window.count:
                retrieved = await save_rows(window.request(as_count=False))
                if retrieved != window.count:
                    warnings.append("truncation: window count differs from retrieved rows")
        if sum(window.count for window in windows) != total.count or len(seen) != total.count:
            warnings.append("truncation: total, window counts and unique IDs do not reconcile")
            # An unfiltered bounded fallback can retain rows lacking queryable dates.
            await save_rows(StudyRecordsRequest(as_count=False, limit=1000))
        return len(seen), list(dict.fromkeys(warnings))
