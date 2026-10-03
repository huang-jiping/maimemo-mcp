"""Configuration models shared by runtime packages without cross-module coupling."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from maimemo.database_url import parse_database_url

DEFAULT_TODAY_INTERVAL_MINUTES = 30
DEFAULT_RECORDS_INTERVAL_MINUTES = 120


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    database_url: str = Field(min_length=1, repr=False)

    @field_validator("database_url")
    @classmethod
    def _database_url(cls, value: str) -> str:
        parse_database_url(value, required_driver="postgresql+psycopg")
        return value

    @classmethod
    def load(cls, environ: Mapping[str, str] | None = None) -> Self:
        source = os.environ if environ is None else environ
        values = {}
        if "MAIMEMO_DATABASE_URL" in source:
            values["database_url"] = source["MAIMEMO_DATABASE_URL"]
        return cls.model_validate(values)


class CoreSettings(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True, hide_input_in_errors=True)

    database: DatabaseSettings
    timezone: ZoneInfo = Field(default_factory=lambda: ZoneInfo("Asia/Shanghai"))
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

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
        values: dict[str, object] = {"database": DatabaseSettings.load(source)}
        if "MAIMEMO_TIMEZONE" in source:
            values["timezone"] = source["MAIMEMO_TIMEZONE"]
        if "MAIMEMO_LOG_LEVEL" in source:
            values["log_level"] = source["MAIMEMO_LOG_LEVEL"]
        return cls.model_validate(values)


class UpstreamCredentialSettings(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    token_file: Path
    token_fingerprint_key_file: Path

    @field_validator("token_file", "token_fingerprint_key_file", mode="before")
    @classmethod
    def _nonempty_path(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("Secret file path must not be empty")
        return value

    @classmethod
    def load(cls, environ: Mapping[str, str] | None = None) -> Self:
        source = os.environ if environ is None else environ
        names = {
            "token_file": "MAIMEMO_TOKEN_FILE",
            "token_fingerprint_key_file": "MAIMEMO_TOKEN_FINGERPRINT_KEY_FILE",
        }
        return cls.model_validate(
            {field: source[name] for field, name in names.items() if name in source}
        )

    def read_maimemo_token(self) -> SecretStr:
        return _read_secret(self.token_file)

    def read_token_fingerprint_key(self) -> SecretStr:
        return _read_secret(self.token_fingerprint_key_file)


class AnalysisIntervals(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    today_interval_minutes: int = Field(default=DEFAULT_TODAY_INTERVAL_MINUTES, gt=0)
    records_interval_minutes: int = Field(default=DEFAULT_RECORDS_INTERVAL_MINUTES, gt=0)

    @property
    def today_interval(self) -> timedelta:
        return timedelta(minutes=self.today_interval_minutes)

    @property
    def records_interval(self) -> timedelta:
        return timedelta(minutes=self.records_interval_minutes)

    @classmethod
    def load(cls, environ: Mapping[str, str] | None = None) -> Self:
        source = os.environ if environ is None else environ
        names = {
            "today_interval_minutes": "MAIMEMO_TODAY_INTERVAL_MINUTES",
            "records_interval_minutes": "MAIMEMO_RECORDS_INTERVAL_MINUTES",
        }
        return cls.model_validate(
            {field: source[name] for field, name in names.items() if name in source}
        )


def _read_secret(path: Path) -> SecretStr:
    try:
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except UnicodeDecodeError:
        value = None
    if value is None:
        raise ValueError("Secret file must be valid UTF-8") from None
    if not value.strip():
        raise ValueError("Secret file must not be empty")
    return SecretStr(value)
