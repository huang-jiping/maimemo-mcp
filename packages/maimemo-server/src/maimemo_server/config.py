"""Server-only database and public HTTP configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Self

from maimemo.config import DatabaseSettings
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator


class ServerSettings(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    database: DatabaseSettings
    host: str = Field(default="0.0.0.0", min_length=1)
    port: int = Field(default=8080, ge=1, le=65535)
    external_base_url: AnyHttpUrl | None = None

    @field_validator("external_base_url")
    @classmethod
    def _https_external_url(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if value is not None and value.scheme != "https":
            raise ValueError("External base URL must use HTTPS")
        return value

    @classmethod
    def load(cls, environ: Mapping[str, str] | None = None) -> Self:
        source = os.environ if environ is None else environ
        names = {
            "host": "MAIMEMO_SERVER_HOST",
            "port": "MAIMEMO_SERVER_PORT",
            "external_base_url": "MAIMEMO_EXTERNAL_BASE_URL",
        }
        values: dict[str, object] = {"database": DatabaseSettings.load(source)}
        values.update({field: source[name] for field, name in names.items() if name in source})
        return cls.model_validate(values)


class MigrationSettings(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    database: DatabaseSettings
    lock_timeout_seconds: int = Field(default=30, ge=1, le=300)
    statement_timeout_seconds: int = Field(default=300, ge=1, le=3600)

    @classmethod
    def load(cls, environ: Mapping[str, str] | None = None) -> Self:
        source = os.environ if environ is None else environ
        names = {
            "lock_timeout_seconds": "MAIMEMO_MIGRATION_LOCK_TIMEOUT_SECONDS",
            "statement_timeout_seconds": "MAIMEMO_MIGRATION_STATEMENT_TIMEOUT_SECONDS",
        }
        values: dict[str, object] = {"database": DatabaseSettings.load(source)}
        values.update({field: source[name] for field, name in names.items() if name in source})
        return cls.model_validate(values)
