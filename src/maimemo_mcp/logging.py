"""Allowlisted JSON logging that never serializes messages, payloads or exception text."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

from maimemo_mcp.config import Settings

_SAFE_TEXT = re.compile(r"^[A-Za-z0-9_./:{}@-]{1,200}$")
_SAFE_EVENT = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FIELDS = frozenset({"endpoint", "latency_ms", "status", "trace_id", "error_class"})


class SafeJsonFormatter(logging.Formatter):
    """Render only an explicit metric/event allowlist, never ``record.getMessage()``."""

    def __init__(self, *, secrets: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if secret)

    def _safe_text(self, value: object) -> str:
        rendered = str(value)
        if any(secret in rendered for secret in self._secrets):
            return "redacted"
        if not _SAFE_TEXT.fullmatch(rendered):
            return "redacted"
        lowered = rendered.lower()
        if "bearer" in lowered or "authorization" in lowered or "password" in lowered:
            return "redacted"
        return rendered

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "safe_event", "log_record")
        if not isinstance(event, str) or not _SAFE_EVENT.fullmatch(event):
            event = "redacted_event"
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat().replace(
                "+00:00", "Z"
            ),
            "level": record.levelname,
            "logger": self._safe_text(record.name),
            "event": event,
        }
        fields = getattr(record, "safe_fields", {})
        if isinstance(fields, dict):
            for name, value in fields.items():
                if name not in _FIELDS or value is None:
                    continue
                if name == "latency_ms" and isinstance(value, int | float):
                    payload[name] = round(max(0.0, float(value)), 3)
                elif name == "status" and isinstance(value, int):
                    payload[name] = value
                else:
                    payload[name] = self._safe_text(value)
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    endpoint: str | None = None,
    latency_ms: float | None = None,
    status: int | str | None = None,
    trace_id: str | None = None,
    error: BaseException | None = None,
    error_class: str | None = None,
    **_discarded_sensitive_fields: Any,
) -> None:
    """Emit an event while deliberately discarding every non-allowlisted value."""

    fields: dict[str, object] = {
        "endpoint": endpoint,
        "latency_ms": latency_ms,
        "status": status,
        "trace_id": trace_id,
        "error_class": error_class or (type(error).__name__ if error is not None else None),
    }
    logger.info(event, extra={"safe_event": event, "safe_fields": fields})


def configure_logging(settings: Settings) -> None:
    """Install one process-wide safe JSON handler at the configured level."""

    secrets = (
        settings.read_maimemo_token().get_secret_value(),
        settings.read_token_fingerprint_key().get_secret_value(),
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(SafeJsonFormatter(secrets=secrets))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, settings.log_level))
