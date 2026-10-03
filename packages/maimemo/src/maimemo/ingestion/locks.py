"""Stable PostgreSQL advisory lock identities shared by repositories and workers."""

from datetime import datetime
from hashlib import sha256

from maimemo.ingestion.normalizers import utc_instant


def advisory_key(identity: str, scheduled_at: datetime) -> int:
    payload = f"maimemo-worker:{identity}:{utc_instant(scheduled_at).isoformat()}".encode()
    return int.from_bytes(sha256(payload).digest()[:8], "big", signed=True)
