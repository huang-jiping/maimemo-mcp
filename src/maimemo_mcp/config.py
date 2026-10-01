"""Environment configuration with explicit, file-based secret access."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class Settings(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    database_url: str = Field(min_length=1, repr=False)
    token_file: Path
    token_fingerprint_key_file: Path
    timezone: ZoneInfo = Field(default_factory=lambda: ZoneInfo("Asia/Shanghai"))
    mcp_host: str = Field(default="0.0.0.0", min_length=1)
    mcp_port: int = Field(default=8000, ge=1, le=65535)
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
