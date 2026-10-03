"""Deterministic JSON identity, scoped to both endpoint and request."""

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def stable_payload_hash(
    endpoint: str, request: Mapping[str, Any], response: Mapping[str, Any]
) -> str:
    canonical = json.dumps(
        {"endpoint": endpoint, "request": dict(request), "response": dict(response)},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
