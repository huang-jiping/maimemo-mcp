"""Literal history fixtures catch fabricated completeness, versions and write leakage."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from maimemo.analysis.models import WeakWordQuery
from maimemo.analysis.service import WeaknessService
from maimemo.ingestion.normalizers import (
    DailyProgressInput,
    DailyWordObservationInput,
    StudyRecordSnapshotInput,
)
from maimemo.storage.models.ingestion import IngestionRun
from maimemo.storage.repositories import StudyHistoryRepository
from maimemo_mcp.config import MCPSettings as Settings
from maimemo_mcp.server import create_mcp_app
from mcp.client import Client
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

AT = datetime(2030, 10, 2, 12, tzinfo=UTC)
TABLES = (
    "vocabulary",
    "ingestion_run",
    "api_snapshot",
    "daily_progress",
    "daily_word_observation",
    "study_record_snapshot",
    "weakness_score",
    "learning_feedback_event",
)


async def counts(engine: AsyncEngine) -> tuple[int, ...]:
    async with engine.connect() as connection:
        values = []
        for table in TABLES:
            values.append(int(await connection.scalar(text(f"SELECT count(*) FROM {table}"))))
        return tuple(values)


async def seed(
    engine: AsyncEngine,
    *,
    at: datetime = AT,
    empty: bool = False,
    status: str = "complete",
    due: datetime | None = None,
    word: str = "Apple",
    feedback: str = "FORGET",
    finished: datetime | None = None,
    tasks: tuple[str, ...] = ("today", "records"),
) -> None:
    factory = async_sessionmaker(engine)
    async with factory() as session, session.begin():
        for task in tasks:
            run = IngestionRun(
                task_type=task,
                started_at=at,
                finished_at=finished or at,
                status=status,
                error_summary="private-payload test-token-only",
            )
            session.add(run)
            await session.flush()
            repo = StudyHistoryRepository(session)
            snapshot = await repo.snapshot(
                task, {}, {"at": at.isoformat(), "word": word, "feedback": feedback}, at, run.id
            )
            if task == "today":
                await repo.progress(
                    DailyProgressInput(at.date(), 0, 0 if empty else 1, 0, at), status
                )
                await repo.today(
                    []
                    if empty
                    else [
                        DailyWordObservationInput(
                            at.date(), word, word, feedback, False, feedback == "FAMILIAR", at
                        )
                    ],
                    snapshot.id,
                )
            else:
                await repo.records(
                    []
                    if empty
                    else [
                        StudyRecordSnapshotInput(
                            word,
                            word,
                            at,
                            at - timedelta(days=30),
                            at - timedelta(days=20),
                            at,
                            due,
                            feedback,
                            5,
                            ["STICKING"],
                        )
                    ],
                    snapshot.id,
                )
    await WeaknessService(factory).recalculate(at)


async def test_dashboard_marks_unavailable_when_today_was_not_initialized(
    workflow_settings: Settings,
    workflow_database: AsyncEngine,
) -> None:
    server = create_mcp_app(workflow_settings, clock=lambda: AT)
    async with Client(server.sdk) as client:
        result = await client.call_tool("get_daily_study_dashboard", {})
        assert result.is_error is False, result.content
        assert result.structured_content is not None
        assert result.structured_content["data"]["progress"] is None
        assert result.structured_content["meta"]["completeness"] == "unavailable"
        assert result.structured_content["meta"]["data_through"] is None
        assert await counts(workflow_database) == (0,) * 8


@pytest.mark.parametrize("status,want", [("partial", "partial"), ("complete", "complete")])
async def test_dashboard_uses_latest_words_and_propagates_health(
    workflow_settings: Settings,
    workflow_database: AsyncEngine,
    status: str,
    want: str,
) -> None:
    await seed(workflow_database, at=AT - timedelta(minutes=10), feedback="FORGET")
    await seed(workflow_database, status=status, feedback="FAMILIAR")
    before = await counts(workflow_database)
    async with Client(create_mcp_app(workflow_settings, clock=lambda: AT).sdk) as client:
        result = await client.call_tool("get_daily_study_dashboard", {})
        assert result.is_error is False, result.content
        assert result.structured_content is not None
        data, meta = result.structured_content["data"], result.structured_content["meta"]
        assert len(data["today_words"]) == 1
        assert data["today_words"][0]["first_feedback"] == "FAMILIAR"
        assert data["progress"]["total_count"] == 1
        assert data["weak_words"][0]["reasons"]
        assert meta["source"] == ["local_history", "local_analysis"]
        assert meta["completeness"] == want
        assert await counts(workflow_database) == before


async def test_word_profile_distinguishes_live_and_local_sources(
    workflow_settings: Settings,
    workflow_database: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed(workflow_database)
    before = await counts(workflow_database)
    calls: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "vocabulary":
            assert request.url.params["spelling"] == " Apple "
            return httpx.Response(
                200, json={"voc": {"id": "live-id", "spelling": "Apple", "extra": 9}}
            )
        assert request.url.params["voc_id"] == "live-id"
        return httpx.Response(200, json={path: []})

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw)
    )
    clock_reads: list[int] = []

    def clock() -> datetime:
        clock_reads.append(len(calls))
        return AT

    async with Client(create_mcp_app(workflow_settings, clock=clock).sdk) as client:
        local = await client.call_tool(
            "get_word_learning_profile", {"request": {"spelling": " Apple "}}
        )
        assert local.is_error is False, local.content
        assert local.structured_content is not None
        assert calls == []
        assert local.structured_content["data"]["profiles"][0]["spelling"] == "Apple"
        live = await client.call_tool(
            "get_word_learning_profile",
            {
                "request": {
                    "spelling": " Apple ",
                    "include_live_content": True,
                }
            },
        )
        assert live.is_error is False, live.content
        assert live.structured_content is not None
        data, meta = live.structured_content["data"], live.structured_content["meta"]
        assert data["spelling"] == " Apple "
        assert data["live_content"]["vocabulary"]["voc"]["id"] == "live-id"
        assert data["live_content"]["vocabulary"]["voc"]["extra"] == 9
        assert data["profiles"][0]["record"]["study_count"] == 5
        assert data["profiles"][0]["weakness"]["reasons"]
        assert meta["source"] == ["local_history", "local_analysis", "maimemo_api"]
        assert meta["completeness"] == "partial"
        assert meta["fetched_at"] == "2030-10-02T12:00:00Z"
        assert clock_reads[-1] == 4
        assert await counts(workflow_database) == before


async def test_weak_words_returns_reasons_confidence_and_data_through(
    workflow_settings: Settings,
    workflow_database: AsyncEngine,
) -> None:
    await seed(workflow_database)
    before = await counts(workflow_database)
    async with Client(create_mcp_app(workflow_settings, clock=lambda: AT).sdk) as client:
        result = await client.call_tool(
            "get_weak_words", {"request": {"min_score": 50, "limit": 1}}
        )
        assert result.is_error is False, result.content
        assert result.structured_content is not None
        word = result.structured_content["data"]["words"][0]
        assert word["score"] == 100  # Only recent response + sticking: (35+15)/50*100.
        assert word["factors"]["sticking"] == {"value": 1.0, "weight": 15}
        assert word["reasons"]
        assert 0 < word["confidence"] < 1
        assert word["data_through"] == "2030-10-02"
        assert result.structured_content["meta"]["data_through"] == "2030-10-02"
        assert await counts(workflow_database) == before


@pytest.mark.parametrize("initialized,want", [(False, "unavailable"), (True, "complete")])
async def test_empty_due_review_requires_healthy_complete_records(
    workflow_settings: Settings,
    workflow_database: AsyncEngine,
    initialized: bool,
    want: str,
) -> None:
    if initialized:
        await seed(workflow_database, empty=True)
    before = await counts(workflow_database)
    async with Client(create_mcp_app(workflow_settings, clock=lambda: AT).sdk) as client:
        result = await client.call_tool("get_due_review_overview", {})
        assert result.is_error is False, result.content
        assert result.structured_content is not None
        assert result.structured_content["data"]["buckets"] == []
        assert result.structured_content["data"]["total"] == 0
        assert result.structured_content["meta"]["completeness"] == want
        assert await counts(workflow_database) == before


async def test_due_review_uses_latest_visible_versions_and_shanghai_dates(
    workflow_settings: Settings,
    workflow_database: AsyncEngine,
) -> None:
    await seed(workflow_database, at=AT - timedelta(minutes=10), due=AT - timedelta(days=1))
    await seed(workflow_database, due=datetime(2030, 10, 3, 16, tzinfo=UTC))
    await seed(workflow_database, at=AT + timedelta(hours=1), due=AT - timedelta(days=1))
    await seed(workflow_database, word="Overdue", due=AT - timedelta(days=1))
    before = await counts(workflow_database)
    async with Client(create_mcp_app(workflow_settings, clock=lambda: AT).sdk) as client:
        result = await client.call_tool("get_due_review_overview", {"request": {"days": 2}})
        assert result.is_error is False, result.content
        assert result.structured_content is not None
        assert result.structured_content["data"]["buckets"] == [
            {"study_date": "2030-10-04", "count": 1}
        ]
        assert result.structured_content["data"]["overdue"] == 1
        assert result.structured_content["data"]["total"] == 2
        assert await counts(workflow_database) == before


async def test_health_reports_stale_without_private_payloads(
    workflow_settings: Settings,
    workflow_database: AsyncEngine,
) -> None:
    await seed(workflow_database, at=AT - timedelta(days=1))
    before = await counts(workflow_database)
    async with Client(create_mcp_app(workflow_settings, clock=lambda: AT).sdk) as client:
        result = await client.call_tool("get_learning_data_health", {})
        assert result.is_error is False, result.content
        assert result.structured_content is not None
        assert result.structured_content["meta"]["completeness"] == "stale"
        assert result.structured_content["meta"]["warnings"] == ["today_stale", "records_stale"]
        assert result.structured_content["data"]["tasks"]["today"]["completeness"] == "stale"
        encoded = json.dumps(result.structured_content)
        assert "private-payload" not in encoded
        assert "test-token-only" not in encoded
        assert await counts(workflow_database) == before


async def test_dashboard_without_formal_today_source_is_unavailable(
    workflow_settings: Settings,
    workflow_database: AsyncEngine,
) -> None:
    await seed(workflow_database, tasks=("records",))
    async with Client(create_mcp_app(workflow_settings, clock=lambda: AT).sdk) as client:
        result = await client.call_tool("get_daily_study_dashboard", {})
        assert result.is_error is False, result.content
        assert result.structured_content is not None
        assert result.structured_content["meta"]["completeness"] == "unavailable"
        assert result.structured_content["meta"]["data_through"] is None


async def test_due_unknown_dates_cannot_claim_complete_empty(
    workflow_settings: Settings,
    workflow_database: AsyncEngine,
) -> None:
    await seed(workflow_database)
    async with Client(create_mcp_app(workflow_settings, clock=lambda: AT).sdk) as client:
        result = await client.call_tool("get_due_review_overview", {})
        assert result.is_error is False, result.content
        assert result.structured_content is not None
        assert result.structured_content["data"]["total"] == 0
        assert result.structured_content["meta"]["completeness"] == "partial"
        assert "review_dates_missing" in result.structured_content["meta"]["warnings"]


async def test_weak_score_cutoff_remains_old_after_new_history_without_recalculation(
    workflow_settings: Settings,
    workflow_database: AsyncEngine,
) -> None:
    await seed(workflow_database, at=AT - timedelta(days=1))
    # A new successful sync can exist while its score is not yet refreshed.
    async with workflow_database.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO ingestion_run(id, task_type, started_at, finished_at, status) "
                "VALUES(gen_random_uuid(), 'today', :at, :at, 'complete'), "
                "(gen_random_uuid(), 'records', :at, :at, 'complete')"
            ),
            {"at": AT},
        )
    async with Client(create_mcp_app(workflow_settings, clock=lambda: AT).sdk) as client:
        result = await client.call_tool("get_weak_words", {})
        assert result.is_error is False, result.content
        assert result.structured_content is not None
        assert result.structured_content["meta"]["data_through"] == "2030-10-01"
        assert result.structured_content["meta"]["completeness"] == "partial"
        assert "analysis_outdated" in result.structured_content["meta"]["warnings"]


async def test_profile_finds_its_score_outside_global_top_thousand(
    workflow_settings: Settings,
    workflow_database: AsyncEngine,
) -> None:
    await seed(workflow_database, feedback="FAMILIAR")
    factory = async_sessionmaker(workflow_database)
    async with factory() as session, session.begin():
        run = IngestionRun(task_type="records", started_at=AT, finished_at=AT, status="complete")
        session.add(run)
        await session.flush()
        repo = StudyHistoryRepository(session)
        snapshot = await repo.snapshot("records", {"batch": 2}, {"synthetic": 1000}, AT, run.id)
        await repo.records(
            [
                StudyRecordSnapshotInput(
                    f"high-{index}",
                    f"high-{index}",
                    AT,
                    AT - timedelta(days=30),
                    AT - timedelta(days=20),
                    AT,
                    AT + timedelta(days=7),
                    "FORGET",
                    5,
                    ["STICKING"],
                )
                for index in range(1000)
            ],
            snapshot.id,
        )
    await WeaknessService(factory).recalculate(AT)
    before = await counts(workflow_database)
    async with Client(create_mcp_app(workflow_settings, clock=lambda: AT).sdk) as client:
        result = await client.call_tool(
            "get_word_learning_profile", {"request": {"spelling": "Apple"}}
        )
        assert result.is_error is False, result.content
        assert result.structured_content is not None
        profile = result.structured_content["data"]["profiles"][0]
        assert profile["weakness"] is not None
        assert profile["weakness"]["spelling"] == "Apple"
        assert profile["weakness"]["score"] == 30
        assert await counts(workflow_database) == before


async def advance_health_without_analysis(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO ingestion_run(id, task_type, started_at, finished_at, status) "
                "VALUES(gen_random_uuid(), 'today', :at, :at, 'complete'), "
                "(gen_random_uuid(), 'records', :at, :at, 'complete')"
            ),
            {"at": AT},
        )


@pytest.mark.parametrize(
    "filters",
    [
        {"min_score": 50},
        {"start": "2030-10-02T00:00:00Z"},
        {"end": "2030-09-30T00:00:00Z"},
    ],
)
async def test_empty_weak_filter_keeps_unfiltered_analysis_freshness_and_cutoff(
    workflow_settings: Settings,
    workflow_database: AsyncEngine,
    filters: dict[str, object],
) -> None:
    # Old score is 30; current health must not make its filtered absence current analysis.
    await seed(workflow_database, at=AT - timedelta(days=1), feedback="FAMILIAR")
    await advance_health_without_analysis(workflow_database)
    before = await counts(workflow_database)
    async with Client(create_mcp_app(workflow_settings, clock=lambda: AT).sdk) as client:
        result = await client.call_tool("get_weak_words", {"request": filters})
        assert result.is_error is False, result.content
        assert result.structured_content is not None
        assert result.structured_content["data"]["words"] == []
        meta = result.structured_content["meta"]
        assert meta["completeness"] == "partial"
        assert meta["data_through"] == "2030-10-01"
        assert meta["warnings"] == ["analysis_outdated"]
        assert await counts(workflow_database) == before


async def test_weak_limit_cannot_hide_an_older_low_score_analysis_source(
    workflow_settings: Settings,
    workflow_database: AsyncEngine,
) -> None:
    await seed(workflow_database, at=AT - timedelta(days=1), feedback="FAMILIAR")
    await seed(workflow_database, word="Fresh")
    async with workflow_database.begin() as connection:
        await connection.execute(
            text(
                "DELETE FROM weakness_score WHERE computed_at = :at AND vocabulary_id IN "
                "(SELECT id FROM vocabulary WHERE spelling = 'Apple')"
            ),
            {"at": AT},
        )
    before = await counts(workflow_database)
    async with Client(create_mcp_app(workflow_settings, clock=lambda: AT).sdk) as client:
        result = await client.call_tool("get_weak_words", {"request": {"limit": 1}})
        assert result.is_error is False, result.content
        assert result.structured_content is not None
        assert [word["spelling"] for word in result.structured_content["data"]["words"]] == [
            "Fresh"
        ]
        meta = result.structured_content["meta"]
        assert meta["completeness"] == "partial"
        assert meta["data_through"] == "2030-10-01"
        assert meta["warnings"] == ["analysis_outdated"]
        assert await counts(workflow_database) == before


@pytest.mark.parametrize("history", [False, True])
async def test_no_current_algorithm_score_is_missing_analysis_not_complete_empty(
    workflow_settings: Settings,
    workflow_database: AsyncEngine,
    history: bool,
) -> None:
    await seed(workflow_database, empty=not history)
    if history:
        # Other algorithm versions cannot establish weakness-v1 availability.
        async with workflow_database.begin() as connection:
            await connection.execute(text("UPDATE weakness_score SET algorithm_version='older-v0'"))
    before = await counts(workflow_database)
    async with Client(create_mcp_app(workflow_settings, clock=lambda: AT).sdk) as client:
        result = await client.call_tool("get_weak_words", {})
        assert result.is_error is False, result.content
        assert result.structured_content is not None
        assert result.structured_content["data"]["words"] == []
        meta = result.structured_content["meta"]
        assert meta["completeness"] == "partial"
        assert meta["data_through"] is None
        assert meta["warnings"] == ["analysis_unavailable"]
        assert await counts(workflow_database) == before


async def test_weak_analysis_coverage_includes_unscored_words_before_filtering(
    workflow_settings: Settings,
    workflow_database: AsyncEngine,
) -> None:
    await seed(workflow_database, word="Unscored")
    await seed(workflow_database, word="Scored")
    async with workflow_database.begin() as connection:
        await connection.execute(
            text(
                "DELETE FROM weakness_score WHERE vocabulary_id IN "
                "(SELECT id FROM vocabulary WHERE spelling='Unscored')"
            )
        )
    before = await counts(workflow_database)
    async with Client(create_mcp_app(workflow_settings, clock=lambda: AT).sdk) as client:
        result = await client.call_tool(
            "get_weak_words", {"request": {"min_score": 99, "limit": 1}}
        )
        assert result.is_error is False, result.content
        assert result.structured_content is not None
        assert [word["spelling"] for word in result.structured_content["data"]["words"]] == [
            "Scored"
        ]
        meta = result.structured_content["meta"]
        assert meta["completeness"] == "partial"
        assert "analysis_coverage_partial" in meta["warnings"]
        assert await counts(workflow_database) == before


async def test_worker_persists_scores_visible_through_real_mcp(
    workflow_settings: Settings, workflow_database: AsyncEngine,
) -> None:
    from maimemo.config import AnalysisIntervals
    from maimemo.ingestion.service import StudyIngestionService
    from maimemo_worker.scheduler import Schedule, ScheduledJob
    from maimemo_worker.worker import Worker

    from tests.integration.test_worker_locking import Upstream

    factory = async_sessionmaker(workflow_database, expire_on_commit=False)
    intervals = AnalysisIntervals()
    collector = Worker(
        StudyIngestionService(factory, Upstream()), Schedule(intervals), clock=lambda: AT
    )
    assert (await collector.run_job(ScheduledJob("today", AT), AT)).status == "complete"
    async with Client(create_mcp_app(workflow_settings, clock=lambda: AT).sdk) as client:
        result = await client.call_tool("get_weak_words", {})
        assert not result.is_error, result.content
        assert result.structured_content is not None
        words = result.structured_content["data"]["words"]
        assert [row["spelling"] for row in words] == ["apple"]
        assert words[0]["evidence_through"] == AT.isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize("age,want", [(10, "complete"), (241, "stale")])
async def test_default_intervals_reach_mcp_health_and_analysis(
    workflow_settings: Settings, workflow_database: AsyncEngine,
    age: int, want: str,
) -> None:
    await seed(workflow_database, at=AT - timedelta(minutes=age))
    async with Client(create_mcp_app(workflow_settings, clock=lambda: AT).sdk) as client:
        health = await client.call_tool("get_learning_data_health", {})
        assert health.structured_content is not None
        assert health.structured_content["meta"]["completeness"] == want
        # Use the production dependency factory, not an independently configured scorer.
    from maimemo_mcp.dependencies import open_mcp_dependencies
    async with open_mcp_dependencies(workflow_settings) as deps:
        await deps.weakness.recalculate(AT)
        words = await deps.weakness.list_weak_words(WeakWordQuery(as_of=AT))
        assert ("DATA_QUALITY_STALE" in words[0].reason_codes) == (want == "stale")


async def test_missing_today_remains_unavailable_with_stale_scores(
    workflow_settings: Settings, workflow_database: AsyncEngine,
) -> None:
    await seed(workflow_database, tasks=("records",), at=AT - timedelta(days=1))
    # Recalculate now to persist stale quality on a real score.
    await WeaknessService(async_sessionmaker(workflow_database)).recalculate(AT)
    async with Client(create_mcp_app(workflow_settings, clock=lambda: AT).sdk) as client:
        result = await client.call_tool("get_daily_study_dashboard", {})
        assert result.structured_content is not None
        assert result.structured_content["data"]["weak_words"]
        assert result.structured_content["meta"]["completeness"] == "unavailable"
        assert "DATA_QUALITY_STALE" in result.structured_content["meta"]["warnings"]


async def test_failed_worker_archive_never_reaches_live_or_composite_mcp(
    workflow_settings: Settings, workflow_database: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from maimemo.config import AnalysisIntervals
    from maimemo.ingestion.service import StudyIngestionService
    from maimemo_mcp.dependencies import open_mcp_dependencies
    from maimemo_worker.scheduler import Schedule, ScheduledJob
    from maimemo_worker.worker import Worker

    private = "SYNTHETIC_SCHEMA_ARCHIVE_PRIVATE"
    caplog.set_level(logging.INFO)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "progress": {"finished": 1, "private": private}
        })), **kw
    ))
    async with open_mcp_dependencies(workflow_settings) as deps:
        intervals = AnalysisIntervals()
        collector = Worker(
            StudyIngestionService(deps.sessions, deps.study, weakness=deps.weakness),
            Schedule(intervals),
            clock=lambda: AT,
        )
        assert (await collector.run_job(ScheduledJob("today", AT), AT)).status == "failed"
    async with workflow_database.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM failed_api_snapshot")) == 1
    async with Client(create_mcp_app(workflow_settings, clock=lambda: AT).sdk) as client:
        for tool in ("get_study_progress", "get_daily_study_dashboard", "get_learning_data_health"):
            result = await client.call_tool(tool, {})
            assert private not in repr(result)
        live = await client.call_tool("get_study_progress", {})
        assert live.is_error
    assert private not in caplog.text
    async with workflow_database.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM failed_api_snapshot")) == 1
