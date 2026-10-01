"""Seven live memo-content and vocabulary adapters."""

from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from pydantic import Field

from maimemo_mcp.maimemo_client.memo_content import ListNotepadsRequest, QueryVocabularyRequest
from maimemo_mcp.maimemo_client.models import (
    GetNotepadResponse,
    InterpretationsResponse,
    ListNotepadsResponse,
    NotesResponse,
    PhrasesResponse,
    QueryVocabularyResponse,
    VocabularyResponse,
)
from maimemo_mcp.mcp_server.dependencies import Dependencies
from maimemo_mcp.mcp_server.envelopes import ToolEnvelope
from maimemo_mcp.mcp_server.tools.common import READ_ONLY, Clock, live_result

ToolContext = Context[Dependencies, Any]
type VocabularyID = Annotated[str, Field(description="Maimemo vocabulary ID, not word spelling.")]


def register(server: MCPServer[Dependencies], clock: Clock) -> None:
    @server.tool(annotations=READ_ONLY)
    async def get_interpretations(
        voc_id: VocabularyID,
        ctx: ToolContext,
    ) -> ToolEnvelope[InterpretationsResponse]:
        """Read interpretations by vocabulary ID; resolve spelling with get_vocabulary first."""
        data = await ctx.request_context.lifespan_context.memo_content.get_interpretations(voc_id)
        return live_result(data, clock)

    @server.tool(annotations=READ_ONLY)
    async def get_notes(voc_id: VocabularyID, ctx: ToolContext) -> ToolEnvelope[NotesResponse]:
        """Read live notes for a vocabulary ID, not spelling."""
        data = await ctx.request_context.lifespan_context.memo_content.get_notes(voc_id)
        return live_result(data, clock)

    @server.tool(annotations=READ_ONLY)
    async def list_notepads(
        request: Annotated[
            ListNotepadsRequest,
            Field(description="Optional upstream limit/offset pagination or notepad IDs."),
        ],
        ctx: ToolContext,
    ) -> ToolEnvelope[ListNotepadsResponse]:
        """List live notepad summaries, optionally paginated or selected by notepad IDs."""
        data = await ctx.request_context.lifespan_context.memo_content.list_notepads(request)
        return live_result(data, clock)

    @server.tool(annotations=READ_ONLY)
    async def get_notepad(
        notepad_id: Annotated[str, Field(description="Notepad OpenAPI ID, not its title.")],
        ctx: ToolContext,
    ) -> ToolEnvelope[GetNotepadResponse]:
        """Read live notepad content and parsed list by notepad ID, not title."""
        data = await ctx.request_context.lifespan_context.memo_content.get_notepad(notepad_id)
        return live_result(data, clock)

    @server.tool(annotations=READ_ONLY)
    async def get_phrases(voc_id: VocabularyID, ctx: ToolContext) -> ToolEnvelope[PhrasesResponse]:
        """Read live phrases for a vocabulary ID, not spelling."""
        data = await ctx.request_context.lifespan_context.memo_content.get_phrases(voc_id)
        return live_result(data, clock)

    @server.tool(annotations=READ_ONLY)
    async def get_vocabulary(
        spelling: Annotated[str, Field(description="Word spelling, preserved as supplied.")],
        ctx: ToolContext,
    ) -> ToolEnvelope[VocabularyResponse]:
        """Resolve spelling to live vocabulary data/ID; the upstream parameter is spelling."""
        # The established client argument is named voc_id but aliases upstream spelling.
        data = await ctx.request_context.lifespan_context.memo_content.get_vocabulary(spelling)
        return live_result(data, clock)

    @server.tool(annotations=READ_ONLY)
    async def query_vocabulary(
        request: Annotated[
            QueryVocabularyRequest,
            Field(
                description="Spellings OR vocabulary IDs; mutually exclusive, at most 1000 each."
            ),
        ],
        ctx: ToolContext,
    ) -> ToolEnvelope[QueryVocabularyResponse]:
        """Query live vocabulary by spellings or IDs, mutually exclusive; upstream max 1000 each."""
        data = await ctx.request_context.lifespan_context.memo_content.query_vocabulary(request)
        return live_result(data, clock)
