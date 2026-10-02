"""Read workflows over internal services; live content is explicitly opt-in."""

from dataclasses import dataclass, fields, replace
from datetime import date, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp_types import ToolAnnotations
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, field_validator

from maimemo_mcp.analysis.models import WeaknessAnalysisState, WeaknessResult, WeakWordQuery
from maimemo_mcp.feedback.models import normalize_spelling
from maimemo_mcp.ingestion.normalizers import SHANGHAI, utc_instant
from maimemo_mcp.maimemo_client.models import (
    InterpretationsResponse,
    NotesResponse,
    PhrasesResponse,
    VocabularyResponse,
)
from maimemo_mcp.mcp_server.dependencies import Dependencies
from maimemo_mcp.mcp_server.envelopes import Completeness, ToolEnvelope, ToolMeta
from maimemo_mcp.mcp_server.tools.common import Clock
from maimemo_mcp.storage.repositories import DataHealth, StudyHistoryRepository

ToolContext = Context[Dependencies, Any]
LOCAL_READ = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
CONTENT_READ = LOCAL_READ.model_copy(update={"open_world_hint": True})


class WorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class WordProfileRequest(WorkflowModel):
    spelling: str = Field(min_length=1, max_length=500)
    include_live_content: StrictBool = False

    @field_validator("spelling")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not normalize_spelling(value):
            raise ValueError("Spelling must not be blank")
        return value


class WeakWordsRequest(WeakWordQuery):
    # The inherited range/threshold/limit validation remains the one query contract.
    as_of: AwareDatetime | None = None  # type: ignore[assignment]


class DueReviewRequest(WorkflowModel):
    days: int = Field(default=7, ge=1, le=366)


@dataclass(frozen=True)
class WeakWordView(WeaknessResult):
    data_through: date | None = None


class ProgressView(WorkflowModel):
    completed_count: int | None
    total_count: int | None
    study_seconds: int | None
    completeness: Completeness
    first_observed_at: datetime
    last_observed_at: datetime


class TodayWordView(WorkflowModel):
    vocabulary_id: UUID
    maimemo_id: str
    spelling: str
    first_feedback: str | None
    is_new: bool | None
    is_complete: bool | None


class DashboardView(WorkflowModel):
    study_date: date
    progress: ProgressView | None
    today_words: list[TodayWordView]
    weak_words: list[WeakWordView]


class RecordView(WorkflowModel):
    added_at: datetime | None
    first_studied_at: datetime | None
    last_studied_at: datetime | None
    next_study_at: datetime | None
    last_feedback: str | None
    study_count: int | None
    tags: list[str] | None


class LocalProfile(WorkflowModel):
    vocabulary_id: UUID
    maimemo_id: str
    spelling: str
    record: RecordView | None
    weakness: WeakWordView | None


class LiveContent(WorkflowModel):
    vocabulary: VocabularyResponse
    interpretations: InterpretationsResponse
    notes: NotesResponse
    phrases: PhrasesResponse


class ProfileView(WorkflowModel):
    # Echoes WordProfileRequest.spelling, so the output contract retains its proven bounds.
    spelling: str = Field(min_length=1, max_length=500)
    profiles: list[LocalProfile]
    live_content: LiveContent | None = None


class WeakWordsView(WorkflowModel):
    words: list[WeakWordView]


class ReviewBucket(WorkflowModel):
    study_date: date
    count: int


class DueReviewView(WorkflowModel):
    study_date: date
    through_date: date
    overdue: int
    buckets: list[ReviewBucket]
    total: int


def weak_view(result: WeaknessResult) -> WeakWordView:
    return WeakWordView(
        **{field.name: getattr(result, field.name) for field in fields(result)},
        data_through=(
            result.evidence_through.astimezone(SHANGHAI).date()
            if result.evidence_through is not None
            else None
        ),
    )


def health_codes(health: DataHealth, tasks: tuple[str, ...]) -> list[str]:
    warnings = [
        f"{name}_{health.tasks[name].completeness}"
        for name in tasks
        if health.tasks[name].completeness != "complete"
    ]
    if health.missing_dates:
        warnings.append("missing_learning_dates")
    return warnings


def local_meta(
    health: DataHealth,
    at: datetime,
    *,
    tasks: tuple[str, ...] = ("today", "records"),
    analysis: bool = False,
) -> ToolMeta:
    states = [health.tasks[name].completeness for name in tasks]
    state: Literal["complete", "partial", "stale", "unavailable"] = "complete"
    if all(value == "unavailable" for value in states):
        state = "unavailable"
    elif "stale" in states:
        state = "stale"
    elif any(value != "complete" for value in states) or health.missing_dates:
        state = "partial"
    through = [health.tasks[name].data_through for name in tasks]
    return ToolMeta(
        source=["local_history", "local_analysis"] if analysis else ["local_history"],
        fetched_at=at,
        data_through=(
            min(value.astimezone(SHANGHAI).date() for value in through if value is not None)
            if all(value is not None for value in through)
            else None
        ),
        completeness=Completeness(state),
        warnings=health_codes(health, tasks),
    )


def incomplete(meta: ToolMeta, code: str) -> None:
    if meta.completeness == Completeness.COMPLETE:
        meta.completeness = Completeness.PARTIAL
    if code not in meta.warnings:
        meta.warnings.append(code)


def score_metadata(meta: ToolMeta, words: list[WeakWordView], health: DataHealth) -> None:
    dates = [word.data_through for word in words]
    if words:
        latest_source = max(
            (task.data_through for task in health.tasks.values() if task.data_through is not None),
            default=None,
        )
        if latest_source is not None and any(word.computed_at < latest_source for word in words):
            incomplete(meta, "analysis_outdated")
        if any(value is None for value in dates):
            meta.data_through = None
            incomplete(meta, "analysis_cutoff_unavailable")
        elif meta.data_through is not None:
            meta.data_through = min(
                meta.data_through, *(value for value in dates if value is not None)
            )
        for word in words:
            for code, state in (
                ("DATA_QUALITY_PARTIAL", Completeness.PARTIAL),
                ("DATA_QUALITY_STALE", Completeness.STALE),
                ("DATA_QUALITY_UNAVAILABLE", Completeness.UNAVAILABLE),
            ):
                if code in word.reason_codes:
                    incomplete(meta, code)
                    if state == Completeness.STALE:
                        meta.completeness = state


def analysis_metadata(meta: ToolMeta, state: WeaknessAnalysisState, health: DataHealth) -> None:
    if state.score_count == 0:
        meta.data_through = None
        incomplete(meta, "analysis_unavailable")
        return
    if state.unscored_word_count:
        incomplete(meta, "analysis_coverage_partial")
    latest_source = max(
        (task.data_through for task in health.tasks.values() if task.data_through is not None),
        default=None,
    )
    if (
        latest_source is not None
        and state.oldest_computed_at is not None
        and state.oldest_computed_at < latest_source
    ):
        incomplete(meta, "analysis_outdated")
    if state.missing_cutoff:
        meta.data_through = None
        incomplete(meta, "analysis_cutoff_unavailable")
    elif meta.data_through is not None and state.data_through is not None:
        meta.data_through = min(meta.data_through, state.data_through.astimezone(SHANGHAI).date())
    for code in state.quality_codes:
        incomplete(meta, code)
        if code == "DATA_QUALITY_STALE":
            meta.completeness = Completeness.STALE


def register(server: MCPServer[Dependencies], clock: Clock) -> None:
    @server.tool(annotations=LOCAL_READ)
    async def get_daily_study_dashboard(ctx: ToolContext) -> ToolEnvelope[DashboardView]:
        """Read today's Shanghai progress, latest observed words and top 10 local scores.

        Missing/uninitialized or stale streams remain explicit. Never calls live APIs.
        """
        deps, at = ctx.request_context.lifespan_context, utc_instant(clock())
        day = at.astimezone(SHANGHAI).date()
        async with deps.sessions() as session:
            repo = StudyHistoryRepository(session)
            health = await repo.get_data_health(at)
            progress = await repo.daily_progress(day, at)
            today = await repo.latest_today_words(day, at)
        scores = [
            weak_view(word)
            for word in await deps.weakness.list_weak_words(WeakWordQuery(as_of=at, limit=10))
        ]
        meta = local_meta(health, at, analysis=True)
        if health.tasks["today"].completeness == "unavailable":
            meta.completeness = Completeness.UNAVAILABLE
            meta.data_through = None
        if progress is None:
            incomplete(meta, "today_progress_unavailable")
        elif progress.completeness != "complete":
            incomplete(meta, "today_progress_partial")
        if today and not scores:
            incomplete(meta, "analysis_unavailable")
        score_metadata(meta, scores, health)
        data = DashboardView(
            study_date=day,
            progress=ProgressView.model_validate(progress) if progress else None,
            today_words=[
                TodayWordView(
                    vocabulary_id=word.id,
                    maimemo_id=word.maimemo_id,
                    spelling=word.spelling,
                    first_feedback=row.first_feedback,
                    is_new=row.is_new,
                    is_complete=row.is_complete,
                )
                for word, row in today
            ],
            weak_words=scores,
        )
        return ToolEnvelope(data=data, meta=meta)

    @server.tool(annotations=CONTENT_READ)
    async def get_word_learning_profile(
        request: WordProfileRequest,
        ctx: ToolContext,
    ) -> ToolEnvelope[ProfileView]:
        """Read profiles matching spelling from local history; preserve original spellings.

        include_live_content=true explicitly fetches vocabulary, interpretations, notes
        and phrases from Maimemo; live content never creates local history or word links.
        """
        deps, at = ctx.request_context.lifespan_context, utc_instant(clock())
        async with deps.sessions() as session:
            repo = StudyHistoryRepository(session)
            health = await repo.get_data_health(at)
            words = await repo.local_words(request.spelling, at)
            records = {word.id: row for word, row in await repo.latest_records(at)}
        scores = {
            word.vocabulary_id: weak_view(word)
            for word in await deps.weakness.list_weak_words(
                WeakWordQuery(as_of=at, limit=1000),
                vocabulary_ids=[word.id for word in words],
            )
        }
        profiles = [
            LocalProfile(
                vocabulary_id=word.id,
                maimemo_id=word.maimemo_id,
                spelling=word.spelling,
                record=RecordView.model_validate(records[word.id]) if word.id in records else None,
                weakness=scores.get(word.id),
            )
            for word in words
        ]
        meta = local_meta(health, at, analysis=True)
        if not profiles:
            incomplete(meta, "word_history_unavailable")
        if any(profile.record is None or profile.weakness is None for profile in profiles):
            incomplete(meta, "word_profile_partial")
        score_metadata(meta, [p.weakness for p in profiles if p.weakness is not None], health)
        content = None
        if request.include_live_content:
            vocabulary = await deps.memo_content.get_vocabulary(request.spelling)
            identity = vocabulary.voc.id
            if not identity.strip():
                raise ValueError("Live vocabulary has no usable identity")
            content = LiveContent(
                vocabulary=vocabulary,
                interpretations=await deps.memo_content.get_interpretations(identity),
                notes=await deps.memo_content.get_notes(identity),
                phrases=await deps.memo_content.get_phrases(identity),
            )
            meta.source.append("maimemo_api")
            meta.fetched_at = utc_instant(clock())
            incomplete(meta, "live_scope_only")
        return ToolEnvelope(
            data=ProfileView(spelling=request.spelling, profiles=profiles, live_content=content),
            meta=meta,
        )

    @server.tool(annotations=LOCAL_READ)
    async def get_weak_words(
        ctx: ToolContext,
        request: WeakWordsRequest | None = None,
    ) -> ToolEnvelope[WeakWordsView]:
        """Read persisted latest weakness scores with factors, reasons and evidence cutoff.

        as_of defaults to now; start/end filter evidence overlap, min_score 0..100,
        limit 1..1000. Never recalculates scores or calls upstream.
        """
        deps, at = ctx.request_context.lifespan_context, utc_instant(clock())
        inputs = request.model_dump(exclude_none=True) if request else {}
        query = WeakWordQuery(**{**inputs, "as_of": inputs.get("as_of", at)})
        async with deps.sessions() as session:
            repo = StudyHistoryRepository(session)
            health = await repo.get_data_health(query.as_of)
        state = await deps.weakness.get_analysis_state(query.as_of)
        words = [weak_view(word) for word in await deps.weakness.list_weak_words(query)]
        meta = local_meta(health, at, analysis=True)
        analysis_metadata(meta, state, health)
        return ToolEnvelope(data=WeakWordsView(words=words), meta=meta)

    @server.tool(annotations=LOCAL_READ)
    async def get_due_review_overview(
        ctx: ToolContext,
        request: DueReviewRequest | None = None,
    ) -> ToolEnvelope[DueReviewView]:
        """Read latest visible local review dates, overdue count and future day buckets.

        days 1..366 (default 7), inclusive through Shanghai today + days. Missing dates
        remain partial; healthy complete records can legitimately return zero items.
        """
        deps, at = ctx.request_context.lifespan_context, utc_instant(clock())
        day = at.astimezone(SHANGHAI).date()
        through = day + timedelta(days=request.days if request else 7)
        async with deps.sessions() as session:
            repo = StudyHistoryRepository(session)
            health = await repo.get_data_health(at)
            records = await repo.latest_records(at)
        meta = local_meta(health, at, tasks=("records",))
        buckets: dict[date, int] = {}
        overdue = 0
        for _, row in records:
            if row.next_study_at is None:
                incomplete(meta, "review_dates_missing")
                continue
            scheduled = row.next_study_at.astimezone(SHANGHAI).date()
            if scheduled < day:
                overdue += 1
            elif scheduled <= through:
                buckets[scheduled] = buckets.get(scheduled, 0) + 1
        return ToolEnvelope(
            data=DueReviewView(
                study_date=day,
                through_date=through,
                overdue=overdue,
                buckets=[
                    ReviewBucket(study_date=day, count=count)
                    for day, count in sorted(buckets.items())
                ],
                total=overdue + sum(buckets.values()),
            ),
            meta=meta,
        )

    @server.tool(annotations=LOCAL_READ)
    async def get_learning_data_health(ctx: ToolContext) -> ToolEnvelope[DataHealth]:
        """Read local synchronization freshness, missing learning dates and safe task status.

        No raw snapshots, exception text, credentials or upstream calls are returned.
        """
        deps, at = ctx.request_context.lifespan_context, utc_instant(clock())
        async with deps.sessions() as session:
            health = await StudyHistoryRepository(session).get_data_health(at)
        meta = local_meta(health, at)
        return ToolEnvelope(data=replace(health, warnings=meta.warnings), meta=meta)
