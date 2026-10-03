"""Three atomic study reads; no initialization/history inference or persistence."""

from typing import Annotated, Any

from maimemo.api_client.models import (
    StudyProgressResponse,
    StudyRecordsResponse,
    TodayItemsResponse,
)
from maimemo.api_client.study import StudyRecordsRequest, TodayItemsRequest
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from pydantic import Field

from maimemo_mcp.mcp_server.dependencies import Dependencies
from maimemo_mcp.mcp_server.envelopes import ToolEnvelope
from maimemo_mcp.mcp_server.tools.common import READ_ONLY, Clock, live_result

ToolContext = Context[Dependencies, Any]


def register(server: MCPServer[Dependencies], clock: Clock) -> None:
    @server.tool(annotations=READ_ONLY)
    async def get_study_progress(ctx: ToolContext) -> ToolEnvelope[StudyProgressResponse]:
        """Read live study progress. Beta API requires App automatic sync.

        Empty/zero data cannot establish initialization or complete history.
        """
        data = await ctx.request_context.lifespan_context.study.get_progress()
        return live_result(data, clock, data.parsing_warnings)

    @server.tool(annotations=READ_ONLY)
    async def get_today_items(
        request: Annotated[
            TodayItemsRequest,
            Field(
                description=(
                    "Vocabulary IDs OR spellings (max 1000 each, mutually exclusive); "
                    "these override other filters. limit max 1000, upstream default 50, "
                    "ordered by study order."
                )
            ),
        ],
        ctx: ToolContext,
    ) -> ToolEnvelope[TodayItemsResponse]:
        """Read today's items by IDs or spellings. Upstream max 1000, default limit 50.

        Beta requires App initialization today and automatic sync; empty data cannot
        establish initialization/history completeness.
        """
        data = await ctx.request_context.lifespan_context.study.get_today_items(request)
        return live_result(data, clock, data.parsing_warnings)

    @server.tool(annotations=READ_ONLY)
    async def query_study_records(
        request: Annotated[
            StudyRecordsRequest,
            Field(
                description=(
                    "Vocabulary IDs OR spellings (spellings max 1000), mutually exclusive "
                    "and override other filters; next_study_date start/end inclusive Beijing time; "
                    "tags STICKING; as_count counts only. limit max 1000, upstream default 50, "
                    "ordered by next study date."
                )
            ),
        ],
        ctx: ToolContext,
    ) -> ToolEnvelope[StudyRecordsResponse]:
        """Query records or count with as_count. IDs differ from spellings.

        Spelling list and limit max 1000, upstream limit default 50. A limited response
        cannot establish complete account history; beta requires App automatic sync.
        """
        data = await ctx.request_context.lifespan_context.study.query_records(request)
        return live_result(data, clock, data.parsing_warnings)
