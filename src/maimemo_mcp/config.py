"""Environment configuration with explicit, file-based secret access."""

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

_HOSTNAME = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)


class Settings(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    database_url: str = Field(min_length=1, repr=False)
    token_file: Path
    token_fingerprint_key_file: Path
    timezone: ZoneInfo = Field(default_factory=lambda: ZoneInfo("Asia/Shanghai"))
    mcp_host: str = Field(default="0.0.0.0", min_length=1)
    mcp_port: int = Field(default=8000, ge=1, le=65535)
    mcp_allowed_hosts: tuple[str, ...] = ()
    today_interval_minutes: int = Field(default=30, gt=0)
    records_interval_minutes: int = Field(default=120, gt=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    openapi_drift_state_file: Path = Path("var/openapi-drift.json")

    @field_validator(
        "token_file", "token_fingerprint_key_file", "openapi_drift_state_file", mode="before"
    )
    @classmethod
    def _nonempty_path(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("Secret file path must not be empty")
        return value

    @field_validator("timezone", mode="before")
    @classmethod
    def _timezone(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                return ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError("Invalid timezone") from exc
        return value

    @field_validator("mcp_allowed_hosts", mode="before")
    @classmethod
    def _mcp_allowed_hosts(cls, value: object) -> object:
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
        values = {
            name: source[f"MAIMEMO_{name.upper()}"]
            for name in cls.model_fields
            if f"MAIMEMO_{name.upper()}" in source
        }
        return cls.model_validate(values)

    def read_maimemo_token(self) -> SecretStr:
        return _read_secret(self.token_file)

    def read_token_fingerprint_key(self) -> SecretStr:
        return _read_secret(self.token_fingerprint_key_file)


def _read_secret(path: Path) -> SecretStr:
    try:
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except UnicodeDecodeError:
        value = None
    # Raise outside the handler so __context__ cannot retain secret bytes.
    if value is None:
        raise ValueError("Secret file must be valid UTF-8") from None
    if not value.strip():
        raise ValueError("Secret file must not be empty")
    return SecretStr(value)
