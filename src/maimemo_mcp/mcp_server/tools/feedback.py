"""Explicit evidence input maps onto the append-only local feedback service."""

from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp_types import ToolAnnotations
from pydantic import Field, StrictBool

from maimemo_mcp.feedback.models import (
    EvidenceType,
    FeedbackDirection,
    FeedbackEventView,
    FeedbackRelationType,
    FeedbackWrite,
    RecordFeedbackCommand,
    RetractFeedbackCommand,
)
from maimemo_mcp.ingestion.normalizers import SHANGHAI
from maimemo_mcp.mcp_server.dependencies import Dependencies
from maimemo_mcp.mcp_server.envelopes import Completeness, ToolEnvelope, ToolMeta
from maimemo_mcp.mcp_server.tools.common import Clock

ToolContext = Context[Dependencies, Any]
APPEND_ONLY = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


class RecordConfusionRequest(FeedbackWrite):
    word_a_spelling: str = Field(min_length=1, max_length=500)
    word_b_spelling: str = Field(min_length=1, max_length=500)
    relation_type: FeedbackRelationType
    direction: FeedbackDirection
    evidence_type: EvidenceType
    confirmed_by_user: StrictBool = False

    def command(self) -> RecordFeedbackCommand:
        return RecordFeedbackCommand(
            **self.model_dump(exclude={"confirmed_by_user"}),
            explicit_confirmation=self.confirmed_by_user,
        )


def feedback_result(event: FeedbackEventView) -> ToolEnvelope[FeedbackEventView]:
    # Immutable event time keeps replay envelopes stable under the same idempotency key.
    return ToolEnvelope(
        data=event,
        meta=ToolMeta(
            source=["local_feedback"],
            fetched_at=event.created_at,
            data_through=event.created_at.astimezone(SHANGHAI).date(),
            completeness=Completeness.COMPLETE,
            warnings=[],
        ),
    )


def register(server: MCPServer[Dependencies], clock: Clock) -> None:
    @server.tool(annotations=APPEND_ONLY)
    async def record_confusion_feedback(
        request: RecordConfusionRequest,
        ctx: ToolContext,
    ) -> ToolEnvelope[FeedbackEventView]:
        """Append explicit local confusion evidence; idempotency_key is required.

        USER_CONFIRMED requires confirmed_by_user=true. QUIZ_OBSERVED requires false
        and only a real quiz's observed wrong answer, never system inference. Preserve
        A_TO_B/B_TO_A/BIDIRECTIONAL exactly. Unknown words remain UNRESOLVED.
        """
        event = await ctx.request_context.lifespan_context.feedback.record(request.command())
        return feedback_result(event)

    @server.tool(annotations=APPEND_ONLY)
    async def retract_feedback(
        request: RetractFeedbackCommand,
        ctx: ToolContext,
    ) -> ToolEnvelope[FeedbackEventView]:
        """Append a retraction referencing an existing confusion event; never delete it.

        idempotency_key and source_agent are required. Replays return the retained event;
        conflicting keys and an already retracted target are rejected.
        """
        event = await ctx.request_context.lifespan_context.feedback.retract(request)
        return feedback_result(event)
