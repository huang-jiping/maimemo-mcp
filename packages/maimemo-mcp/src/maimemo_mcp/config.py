"""MCP-only configuration assembled from shared core settings."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Self

from maimemo.config import CoreSettings, UpstreamCredentialSettings
from pydantic import BaseModel, ConfigDict, Field, field_validator

_HOSTNAME = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)


class MCPSettings(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    core: CoreSettings
    upstream: UpstreamCredentialSettings
    host: str = Field(default="0.0.0.0", min_length=1)
    port: int = Field(default=8000, ge=1, le=65535)
    allowed_hosts: tuple[str, ...] = ()
    drift_state_file: Path = Path("var/openapi-drift.json")

    @field_validator("drift_state_file", mode="before")
    @classmethod
    def _nonempty_path(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("Drift state file path must not be empty")
        return value

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _allowed_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            value = tuple(part.strip().lower() for part in value.split(",") if part.strip())
        if not isinstance(value, (tuple, list)):
            raise ValueError("MCP allowed hosts must be a comma-separated hostname list")
        hosts = tuple(str(host).strip().lower() for host in value)
        if any(not _HOSTNAME.fullmatch(host) for host in hosts):
            raise ValueError(
                "MCP allowed hosts must contain exact hostnames without ports or wildcards"
            )
        if len(set(hosts)) != len(hosts):
            raise ValueError("MCP allowed hosts must not contain duplicates")
        return hosts

    @classmethod
    def load(cls, environ: Mapping[str, str] | None = None) -> Self:
        source = os.environ if environ is None else environ
        names = {
            "host": "MAIMEMO_MCP_HOST",
            "port": "MAIMEMO_MCP_PORT",
            "allowed_hosts": "MAIMEMO_MCP_ALLOWED_HOSTS",
            "drift_state_file": "MAIMEMO_OPENAPI_DRIFT_STATE_FILE",
        }
        values: dict[str, object] = {
            "core": CoreSettings.load(source),
            "upstream": UpstreamCredentialSettings.load(source),
        }
        values.update({field: source[name] for field, name in names.items() if name in source})
        return cls.model_validate(values)
