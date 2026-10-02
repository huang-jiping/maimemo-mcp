"""Shared live-result metadata, without guessing initialization or account history."""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from mcp_types import ToolAnnotations

from maimemo_mcp.maimemo_client.models import ParsingWarning, ResponseModel
from maimemo_mcp.mcp_server.envelopes import Completeness, ToolEnvelope, ToolMeta

type Clock = Callable[[], datetime]

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def live_result[T: ResponseModel](
    data: T, clock: Clock, warnings: Sequence[ParsingWarning] = ()
) -> ToolEnvelope[T]:
    # Only the model's trusted warning code is exposed; unknown feedback remains in data.
    # Deduplicate diagnostics without copying arbitrary upstream fields into metadata.
    codes = list(dict.fromkeys(warning.code for warning in warnings))
    return ToolEnvelope(
        data=data,
        meta=ToolMeta(
            source=["maimemo_api"],
            fetched_at=clock(),
            data_through=None,
            completeness=Completeness.PARTIAL,
            warnings=["live_scope_only", *codes],
        ),
    )
