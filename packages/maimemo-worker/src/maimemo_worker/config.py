"""Worker-only configuration assembled from shared core settings."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Self

from maimemo.config import AnalysisIntervals, CoreSettings, UpstreamCredentialSettings
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field


class WorkerSettings(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    core: CoreSettings
    upstream: UpstreamCredentialSettings
    intervals: AnalysisIntervals
    schema_wait_timeout_seconds: int = Field(default=60, ge=1, le=600)
    drift_state_file: Path = Path("var/openapi-drift.json")
    pinned_openapi_file: Path = Path("openapi/maimemo-api.yaml")
    openapi_url: AnyHttpUrl = AnyHttpUrl("https://open.maimemo.com/api_bundle.yaml")

    @classmethod
    def load(cls, environ: Mapping[str, str] | None = None) -> Self:
        source = os.environ if environ is None else environ
        names = {
            "schema_wait_timeout_seconds": "MAIMEMO_SCHEMA_WAIT_TIMEOUT_SECONDS",
            "drift_state_file": "MAIMEMO_OPENAPI_DRIFT_STATE_FILE",
            "pinned_openapi_file": "MAIMEMO_OPENAPI_PINNED_FILE",
            "openapi_url": "MAIMEMO_OPENAPI_URL",
        }
        values: dict[str, object] = {
            "core": CoreSettings.load(source),
            "upstream": UpstreamCredentialSettings.load(source),
            "intervals": AnalysisIntervals.load(source),
        }
        values.update({field: source[name] for field, name in names.items() if name in source})
        return cls.model_validate(values)
