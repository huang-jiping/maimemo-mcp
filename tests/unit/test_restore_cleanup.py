"""Restore cleanup preserves the primary failure and records cleanup failure."""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import make_url


class FailingTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def database_argument(self, url: object) -> tuple[str, dict[str, str]]:
        return "postgresql://admin@db.invalid/postgres", {}

    def run(self, name: str, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((name, args))
        if name == "pg_restore":
            raise ValueError("primary restore failure")
        if name == "dropdb":
            raise RuntimeError("cleanup failure")
        return subprocess.CompletedProcess([name, *args], 0, "", "")


def test_cleanup_failure_does_not_mask_primary_restore_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    restore = importlib.import_module("restore_postgres")
    backup = tmp_path / "valid-name.dump"
    backup.write_bytes(b"synthetic")
    tools = FailingTools()

    with pytest.raises(ValueError, match="primary restore failure") as caught:
        restore._restore_new_database(
            tools,
            make_url("postgresql://admin@db.invalid/postgres"),
            "maimemo_restore_deadbeef",
            backup,
        )

    assert [name for name, _ in tools.calls] == ["createdb", "pg_restore", "dropdb"]
    assert caught.value.__notes__ == [
        "Disposable restore cleanup failed with RuntimeError; manual cleanup is required"
    ]
