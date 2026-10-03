"""Seven thin Markji adapters sharing the SDK lifespan clients."""

from typing import Annotated, Any

from maimemo.api_client.markji import (
    ListDecksRequest,
    ListFoldersRequest,
    QueryFilesRequest,
)
from maimemo.api_client.models import (
    GetCardResponse,
    GetChapterResponse,
    GetDeckResponse,
    ListChaptersResponse,
    ListDecksResponse,
    ListFoldersResponse,
    QueryFilesResponse,
)
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from pydantic import Field

from maimemo_mcp.mcp_server.dependencies import Dependencies
from maimemo_mcp.mcp_server.envelopes import ToolEnvelope
from maimemo_mcp.mcp_server.tools.common import READ_ONLY, Clock, live_result

ToolContext = Context[Dependencies, Any]
type DeckID = Annotated[str, Field(description="Markji deck OpenAPI ID, not a deck name.")]


def register(server: MCPServer[Dependencies], clock: Clock) -> None:
    @server.tool(annotations=READ_ONLY)
    async def list_markji_folders(ctx: ToolContext) -> ToolEnvelope[ListFoldersResponse]:
        """List live Markji folders to discover folder IDs; no request parameters."""
        data = await ctx.request_context.lifespan_context.markji.list_folders(ListFoldersRequest())
        return live_result(data, clock)

    @server.tool(annotations=READ_ONLY)
    async def list_markji_decks(
        request: Annotated[
            ListDecksRequest,
            Field(description="Optional offset/limit pagination and folder_id (OpenAPI ID)."),
        ],
        ctx: ToolContext,
    ) -> ToolEnvelope[ListDecksResponse]:
        """List live decks, optionally paginated or filtered by folder ID; returns total."""
        data = await ctx.request_context.lifespan_context.markji.list_decks(request)
        return live_result(data, clock)

    @server.tool(annotations=READ_ONLY)
    async def get_markji_deck(deck: DeckID, ctx: ToolContext) -> ToolEnvelope[GetDeckResponse]:
        """Read one live Markji deck using its OpenAPI ID, not its name."""
        data = await ctx.request_context.lifespan_context.markji.get_deck(deck)
        return live_result(data, clock)

    @server.tool(annotations=READ_ONLY)
    async def list_markji_chapters(
        deck: DeckID,
        ctx: ToolContext,
    ) -> ToolEnvelope[ListChaptersResponse]:
        """List live chapters for a deck OpenAPI ID to discover chapter/card IDs."""
        data = await ctx.request_context.lifespan_context.markji.list_chapters(deck)
        return live_result(data, clock)

    @server.tool(annotations=READ_ONLY)
    async def get_markji_chapter(
        deck: DeckID,
        chapter: Annotated[str, Field(description="Chapter OpenAPI ID, not chapter title.")],
        ctx: ToolContext,
    ) -> ToolEnvelope[GetChapterResponse]:
        """Read a live chapter by deck and chapter OpenAPI IDs, including upstream card data."""
        data = await ctx.request_context.lifespan_context.markji.get_chapter(deck, chapter)
        return live_result(data, clock)

    @server.tool(annotations=READ_ONLY)
    async def get_markji_card(
        deck: DeckID,
        card: Annotated[str, Field(description="Card OpenAPI ID, not word spelling.")],
        ctx: ToolContext,
    ) -> ToolEnvelope[GetCardResponse]:
        """Read a live card by deck and card OpenAPI IDs; preserve content and file metadata."""
        data = await ctx.request_context.lifespan_context.markji.get_card(deck, card)
        return live_result(data, clock)

    @server.tool(annotations=READ_ONLY)
    async def query_markji_files(
        request: Annotated[
            QueryFilesRequest,
            Field(description="File OpenAPI IDs; expires is seconds (upstream default 30 days)."),
        ],
        ctx: ToolContext,
    ) -> ToolEnvelope[QueryFilesResponse]:
        """Query live file links by file IDs; expires is seconds, upstream default 30 days."""
        data = await ctx.request_context.lifespan_context.markji.query_files(request)
        return live_result(data, clock)
