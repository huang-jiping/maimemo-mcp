"""Real PostgreSQL verifies atomic history, versioning and honest record coverage."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from maimemo.api_client.models import (
    StudyProgressResponse,
    StudyRecordsResponse,
    TodayItemsResponse,
)
from maimemo.api_client.study import StudyClient, StudyRecordsRequest, TodayItemsRequest
from maimemo.api_client.transport import MaimemoTransport
from maimemo.ingestion.service import StudyIngestionService, StudyRecordWindowPlanner
from maimemo.storage.models.ingestion import ApiSnapshot, IngestionRun
from maimemo.storage.models.learning import (
    DailyProgress,
    DailyWordObservation,
    StudyRecordSnapshot,
    Vocabulary,
)
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

AT = datetime(2026, 10, 1, 16, 30, tzinfo=UTC)
START = datetime(2026, 10, 1, 16, tzinfo=UTC)
END = datetime(2026, 10, 3, 16, tzinfo=UTC) - timedelta(microseconds=1)


def record(identity: str, next_date: str | None = "2026-10-02T00:00:00+08:00") -> dict[str, Any]:
    row: dict[str, Any] = {
        "voc_id": identity,
        "voc_spelling": "apple",
        "add_date": "2026-10-01T00:00:00+08:00",
        "study_count": 2,
        "tags": "STICKING",
    }
    if next_date is not None:
        row["next_study_date"] = next_date
    return row


class StudyFake:
    """Only replace the upstream boundary; all persistence uses the real DB."""

    def __init__(self) -> None:
        self.progress: dict[str, Any] = {
            "progress": {"finished": 1, "total": 1, "study_time": 60000}
        }
        self.items: dict[str, Any] = {
            "today_items": [
                {
                    "voc_id": "opaque-id",
                    "voc_spelling": "Apple",
                    "order": 1,
                    "first_response": "VAGUE",
                    "is_new": False,
                    "is_finished": True,
                    "future": {"nullable": None},
                }
            ]
        }
        self.rows = [record("opaque-id")]
        self.calls: list[StudyRecordsRequest] = []
        self.total_override: int | None = None
        self.failure: Exception | None = None

    async def get_progress(self) -> StudyProgressResponse:
        return StudyProgressResponse.model_validate(self.progress)

    async def get_today_items(self, request: TodayItemsRequest) -> TodayItemsResponse:
        if self.failure:
            raise self.failure
        return TodayItemsResponse.model_validate(self.items)

    async def query_records(self, request: StudyRecordsRequest) -> StudyRecordsResponse:
        self.calls.append(request)
        rows = self.rows
        if request.next_study_date:
            window = request.next_study_date
            start = datetime.fromisoformat(window.start) if window.start else START
            end = datetime.fromisoformat(window.end) if window.end else END
            rows = [
                row
                for row in rows
                if row.get("next_study_date")
                and start <= datetime.fromisoformat(row["next_study_date"]) <= end
            ]
        count = len(rows)
        if request.next_study_date is None and self.total_override is not None:
            count = self.total_override
        return StudyRecordsResponse.model_validate(
            {
                "records": [] if request.as_count else rows[: request.limit or 50],
                "count": count if request.as_count else 0,
            }
        )


def service(database: AsyncEngine, fake: StudyFake) -> StudyIngestionService:
    return StudyIngestionService(
        async_sessionmaker(database, expire_on_commit=False),
        fake,
        records_range_start=START,
        records_range_end=END,
    )


async def counts(factory: async_sessionmaker[AsyncSession]) -> list[int]:
    async with factory() as session:
        return [
            int(await session.scalar(select(func.count()).select_from(model)) or 0)
            for model in (ApiSnapshot, DailyProgress, DailyWordObservation, StudyRecordSnapshot)
        ]


async def test_repeated_identical_response_creates_one_snapshot(database: AsyncEngine) -> None:
    fake = StudyFake()
    collector = service(database, fake)
    await collector.collect_today(AT)
    result = await collector.collect_today(AT + timedelta(minutes=30))
    assert result.status == "complete"
    assert await counts(collector.session_factory) == [2, 1, 1, 0]
    async with collector.session_factory() as session:
        snapshots = list((await session.scalars(select(ApiSnapshot))).all())
        assert all(row.observation_kind == "BASELINE" for row in snapshots)
        raw = next(row.raw_response for row in snapshots if "today_items" in row.raw_response)
        assert raw == fake.items
        word = await session.scalar(select(Vocabulary))
        assert word is not None and word.maimemo_id == "opaque-id"
        observation = await session.scalar(select(DailyWordObservation))
        assert observation is not None
        assert observation.first_observed_at == AT
        assert observation.last_observed_at == AT + timedelta(minutes=30)


async def test_changed_response_creates_new_observation(database: AsyncEngine) -> None:
    fake = StudyFake()
    collector = service(database, fake)
    await collector.collect_today(AT)
    fake.items["today_items"][0]["first_response"] = "FORGET"
    await collector.collect_today(AT + timedelta(minutes=30))
    assert await counts(collector.session_factory) == [3, 1, 2, 0]
    async with collector.session_factory() as session:
        assert set((await session.scalars(select(DailyWordObservation.first_feedback))).all()) == {
            "VAGUE",
            "FORGET",
        }
        assert "OBSERVATION" in (await session.scalars(select(ApiSnapshot.observation_kind))).all()


async def test_normalization_failure_rolls_back_snapshot_and_history(database: AsyncEngine) -> None:
    fake = StudyFake()
    fake.items["today_items"][0]["voc_spelling"] = ""
    collector = service(database, fake)
    result = await collector.collect_today(AT)
    assert result.status == "failed"
    assert await counts(collector.session_factory) == [0, 0, 0, 0]
    async with collector.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Vocabulary)) == 0
        run = await session.scalar(select(IngestionRun))
        assert run is not None and run.status == "failed"


async def test_failed_run_is_recorded_in_separate_transaction_without_secrets(
    database: AsyncEngine,
) -> None:
    fake = StudyFake()
    fake.failure = ValueError("Bearer secret-token password=private")
    collector = service(database, fake)
    result = await collector.collect_today(AT)
    assert result.status == "failed"
    async with collector.session_factory() as session:
        run = await session.scalar(select(IngestionRun))
        assert run is not None and run.finished_at is not None
        assert "secret-token" not in str(run.error_summary)
        assert "private" not in str(result.warnings)
    fake.failure = None
    await collector.collect_today(AT + timedelta(minutes=30))
    async with collector.session_factory() as session:
        assert set((await session.scalars(select(ApiSnapshot.observation_kind))).all()) == {
            "BASELINE"
        }


async def test_empty_uninitialized_day_does_not_overwrite_valid_day(database: AsyncEngine) -> None:
    fake = StudyFake()
    collector = service(database, fake)
    await collector.collect_today(AT)
    fake.items = {"today_items": []}
    fake.progress = {"progress": {"finished": 0, "total": 0, "study_time": 0}}
    result = await collector.collect_today(AT + timedelta(minutes=30))
    assert result.status == "partial"
    async with collector.session_factory() as session:
        row = await session.scalar(select(DailyProgress))
        assert row is not None and row.completed_count == 1 and row.total_count == 1
        assert row.completeness == "complete"


async def test_record_sync_recursively_splits_ranges_over_one_thousand(
    database: AsyncEngine,
) -> None:
    fake = StudyFake()
    fake.rows = [record(f"a-{i}") for i in range(700)] + [
        record(f"b-{i}", "2026-10-03T00:00:00+08:00") for i in range(700)
    ]
    collector = service(database, fake)
    result = await collector.collect_records(AT)
    assert result.status == "complete"
    assert result.result_count == 1400
    assert fake.calls[0].as_count is True and fake.calls[0].next_study_date is None
    row_calls = [request for request in fake.calls if not request.as_count]
    assert len(row_calls) == 2
    assert all(request.limit == 1000 for request in row_calls)
    assert await counts(collector.session_factory) == [6, 0, 0, 1400]


async def test_single_day_over_one_thousand_is_marked_partial(database: AsyncEngine) -> None:
    fake = StudyFake()
    fake.rows = [record(f"id-{i}") for i in range(1100)]
    collector = service(database, fake)
    result = await collector.collect_records(AT)
    assert result.status == "partial"
    assert result.result_count == 1000
    assert any("truncation" in warning for warning in result.warnings)
    assert len(fake.calls) < 12


@pytest.mark.parametrize("mismatch", ["total", "missing_date", "duplicate"])
async def test_count_or_query_date_mismatch_is_partial(
    database: AsyncEngine,
    mismatch: str,
) -> None:
    fake = StudyFake()
    if mismatch == "total":
        fake.total_override = 2
    elif mismatch == "missing_date":
        fake.rows.append(record("no-date", None))
    else:
        fake.rows.append(record("opaque-id"))
    collector = service(database, fake)
    result = await collector.collect_records(AT)
    assert result.status == "partial"
    assert any("truncation" in warning for warning in result.warnings)
    assert result.result_count >= 1
    if mismatch == "missing_date":
        assert result.result_count == 2


async def test_planner_rejects_reversed_range_without_upstream_call() -> None:
    fake = StudyFake()
    with pytest.raises(ValueError):
        await StudyRecordWindowPlanner(fake).plan(END, START)
    assert fake.calls == []


async def test_empty_day_preserves_nonzero_partial_progress(database: AsyncEngine) -> None:
    fake = StudyFake()
    fake.progress["progress"]["total"] = 2
    collector = service(database, fake)
    assert (await collector.collect_today(AT)).status == "partial"
    fake.progress = {"progress": {"finished": 0, "total": 0, "study_time": 0}}
    fake.items = {"today_items": []}
    await collector.collect_today(AT + timedelta(minutes=30))
    async with collector.session_factory() as session:
        row = await session.scalar(select(DailyProgress))
        assert row is not None and row.completed_count == 1 and row.total_count == 2


async def test_duplicate_today_ids_cannot_claim_complete(database: AsyncEngine) -> None:
    fake = StudyFake()
    fake.items["today_items"].append(fake.items["today_items"][0].copy())
    fake.progress["progress"]["total"] = 2
    result = await service(database, fake).collect_today(AT)
    assert result.status == "partial"
    assert result.result_count == 1


async def test_same_payload_on_new_day_is_observation_not_baseline(database: AsyncEngine) -> None:
    collector = service(database, StudyFake())
    await collector.collect_today(AT)
    await collector.collect_today(AT + timedelta(days=1))
    assert await counts(collector.session_factory) == [4, 2, 2, 0]
    async with collector.session_factory() as session:
        kinds = (await session.scalars(select(ApiSnapshot.observation_kind))).all()
        assert list(kinds).count("BASELINE") == 2
        assert list(kinds).count("OBSERVATION") == 2


async def test_record_rows_are_idempotent_but_changed_response_adds_history(
    database: AsyncEngine,
) -> None:
    fake = StudyFake()
    collector = service(database, fake)
    await collector.collect_records(AT)
    await collector.collect_records(AT + timedelta(minutes=30))
    assert (await counts(collector.session_factory))[3] == 1
    fake.rows[0]["study_count"] = 3
    await collector.collect_records(AT + timedelta(hours=1))
    assert (await counts(collector.session_factory))[3] == 2


async def test_exactly_one_thousand_in_single_day_is_partial(database: AsyncEngine) -> None:
    fake = StudyFake()
    fake.rows = [record(f"id-{i}") for i in range(1000)]
    collector = StudyIngestionService(
        async_sessionmaker(database, expire_on_commit=False),
        fake,
        records_range_start=START,
        records_range_end=START + timedelta(days=1) - timedelta(microseconds=1),
    )
    result = await collector.collect_records(AT)
    assert result.status == "partial" and result.result_count == 1000


async def test_recursive_windows_cover_inclusive_boundaries_without_gaps() -> None:
    fake = StudyFake()
    fake.rows = (
        [record(f"a-{i}") for i in range(700)]
        + [record(f"b-{i}", "2026-10-03T00:00:00+08:00") for i in range(700)]
        + [record(f"c-{i}", "2026-10-04T00:00:00+08:00") for i in range(700)]
        + [record(f"d-{i}", "2026-10-05T00:00:00+08:00") for i in range(700)]
    )
    windows = await StudyRecordWindowPlanner(fake).plan(
        START, START + timedelta(days=4) - timedelta(microseconds=1)
    )
    assert len(windows) == 4
    assert [window.count for window in windows] == [700, 700, 700, 700]
    assert windows[0].range_start == START
    assert windows[-1].range_end == START + timedelta(days=4) - timedelta(microseconds=1)
    assert all(
        left.range_end + timedelta(microseconds=1) == right.range_start
        for left, right in zip(windows, windows[1:], strict=False)
    )


async def test_out_of_window_record_is_preserved_but_marked_partial(database: AsyncEngine) -> None:
    class LeakyStudyFake(StudyFake):
        async def query_records(self, request: StudyRecordsRequest) -> StudyRecordsResponse:
            self.calls.append(request)
            return StudyRecordsResponse.model_validate(
                {
                    "records": [] if request.as_count else self.rows,
                    "count": 1 if request.as_count else 0,
                }
            )

    fake = LeakyStudyFake()
    fake.rows = [record("outside", "2026-11-01T00:00:00+08:00")]
    collector = service(database, fake)
    result = await collector.collect_records(AT)
    assert result.status == "partial" and result.result_count == 1
    assert (await counts(collector.session_factory))[3] == 1


async def test_invalid_record_timestamp_rolls_back_all_count_snapshots(
    database: AsyncEngine,
) -> None:
    fake = StudyFake()
    fake.rows[0]["add_date"] = "not-a-date Bearer secret"
    collector = service(database, fake)
    result = await collector.collect_records(AT)
    assert result.status == "failed" and "secret" not in str(result.warnings)
    assert await counts(collector.session_factory) == [0, 0, 0, 0]


async def test_fake_client_failure_without_http_send_records_zero_attempts(
    database: AsyncEngine,
) -> None:
    class FailingCountStudyFake(StudyFake):
        async def query_records(self, request: StudyRecordsRequest) -> StudyRecordsResponse:
            if request.next_study_date is not None:
                raise ValueError("Bearer private")
            return await super().query_records(request)

    collector = service(database, FailingCountStudyFake())
    result = await collector.collect_records(AT)
    assert result.status == "failed" and result.request_count == 0
    async with collector.session_factory() as session:
        run = await session.scalar(select(IngestionRun))
        assert run is not None and run.request_count == 0


async def test_service_result_does_not_depend_on_session_commit_expiration(
    database: AsyncEngine,
) -> None:
    collector = StudyIngestionService(async_sessionmaker(database), StudyFake())
    result = await collector.collect_today(AT)
    assert result.status == "complete"
    async with collector.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(IngestionRun)) == 1


async def test_record_returning_to_prior_content_refreshes_observed_at(
    database: AsyncEngine,
) -> None:
    fake = StudyFake()
    fake.rows[0]["last_response"] = "FORGET"
    collector = service(database, fake)
    await collector.collect_records(AT)
    async with collector.session_factory() as session:
        first = await session.scalar(select(StudyRecordSnapshot))
        assert first is not None
        first_source = first.source_snapshot_id
        raw = await session.get(ApiSnapshot, first_source)
        assert raw is not None
        first_payload = raw.raw_response
    fake.rows[0]["last_response"] = "FAMILIAR"
    await collector.collect_records(AT + timedelta(minutes=30))
    fake.rows[0]["last_response"] = "FORGET"
    await collector.collect_records(AT + timedelta(hours=1))
    async with collector.session_factory() as session:
        latest = await session.scalar(
            select(StudyRecordSnapshot).order_by(StudyRecordSnapshot.observed_at.desc())
        )
        assert latest is not None and latest.last_feedback == "FORGET"
        assert latest.observed_at == AT + timedelta(hours=1)
        assert latest.source_snapshot_id == first_source
        raw = await session.get(ApiSnapshot, first_source)
        assert raw is not None and raw.fetched_at == AT and raw.raw_response == first_payload
        assert await session.scalar(select(func.count()).select_from(StudyRecordSnapshot)) == 2


async def test_identical_records_refresh_observation_without_new_snapshot(
    database: AsyncEngine,
) -> None:
    collector = service(database, StudyFake())
    await collector.collect_records(AT)
    first_counts = await counts(collector.session_factory)
    await collector.collect_records(AT + timedelta(minutes=30))
    assert await counts(collector.session_factory) == first_counts
    async with collector.session_factory() as session:
        row = await session.scalar(select(StudyRecordSnapshot))
        assert row is not None and row.observed_at == AT + timedelta(minutes=30)


@pytest.mark.parametrize("exhausted", [False, True])
async def test_ingestion_counts_actual_http_attempts_with_retry(
    database: AsyncEngine,
    exhausted: bool,
) -> None:
    class NoWaitLimiter:
        def __init__(self) -> None:
            self.acquisitions = 0

        async def acquire(self, fingerprint: str) -> None:
            self.acquisitions += 1

    attempts = 0
    limiter = NoWaitLimiter()

    async def no_sleep(delay: float) -> None:
        return None

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if exhausted or attempts == 1:
            return httpx.Response(500, text="Bearer synthetic-private-token")
        if request.url.path.endswith("get_study_progress"):
            return httpx.Response(
                200,
                json={
                    "progress": {
                        "finished": 0,
                        "total": 1,
                        "study_time": 0,
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "today_items": [
                    {
                        "voc_id": "opaque",
                        "voc_spelling": "apple",
                        "order": 1,
                        "is_new": True,
                        "is_finished": False,
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
        client = StudyClient(
            MaimemoTransport(
                SecretStr("synthetic-private-token"),
                SecretStr("synthetic-key"),
                limiter,
                client=http_client,
                sleep=no_sleep,
            )
        )
        collector = StudyIngestionService(
            async_sessionmaker(database, expire_on_commit=False), client
        )
        result = await collector.collect_today(AT)
    assert result.status == ("failed" if exhausted else "complete")
    assert attempts == 3 and limiter.acquisitions == 3
    assert result.request_count == 3
    async with collector.session_factory() as session:
        run = await session.scalar(select(IngestionRun))
        assert run is not None and run.request_count == 3
        assert "synthetic-private-token" not in str(run.error_summary)
    assert "synthetic-private-token" not in str(result.warnings)
