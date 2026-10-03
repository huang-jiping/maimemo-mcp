"""Worker-only configuration assembled from shared core settings."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Self

from maimemo.config import AnalysisIntervals, CoreSettings, UpstreamCredentialSettings
from pydantic import BaseModel, ConfigDict


class WorkerSettings(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    core: CoreSettings
    upstream: UpstreamCredentialSettings
    intervals: AnalysisIntervals

    @classmethod
    def load(cls, environ: Mapping[str, str] | None = None) -> Self:
        source = os.environ if environ is None else environ
        return cls(
            core=CoreSettings.load(source),
            upstream=UpstreamCredentialSettings.load(source),
            intervals=AnalysisIntervals.load(source),
        )
