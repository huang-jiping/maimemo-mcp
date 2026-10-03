"""The gate must resolve exactly one shipped migration head without leaking paths."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from maimemo.storage.schema import (
    SchemaDefinitionError,
    SchemaState,
    SchemaStatus,
    expected_schema_revision,
)


def test_shipped_migrations_have_one_expected_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert expected_schema_revision() == "0004"


@pytest.mark.parametrize("heads", [0, 2])
def test_zero_or_multiple_heads_fail_with_safe_message(tmp_path: Path, heads: int) -> None:
    migrations = tmp_path / "private-credential-path" / "migrations"
    versions = migrations / "versions"
    versions.mkdir(parents=True)
    for index in range(heads):
        (versions / f"{index}.py").write_text(
            f"revision = 'head_{index}'\ndown_revision = None\n", encoding="utf-8"
        )
    config = tmp_path / "alembic.ini"
    config.write_text(f"[alembic]\nscript_location = {migrations.as_posix()}\n", encoding="utf-8")

    with pytest.raises(SchemaDefinitionError) as failure:
        expected_schema_revision(config)

    assert str(failure.value) == "schema_definition_invalid"
    assert "private-credential-path" not in repr(failure.value)


def test_unreadable_definition_has_safe_message(tmp_path: Path) -> None:
    with pytest.raises(SchemaDefinitionError, match="^schema_definition_invalid$"):
        expected_schema_revision(tmp_path / "secret" / "missing.ini")


def test_schema_state_cannot_be_mutated() -> None:
    state = SchemaState(("0004",), ("0004",), "0004", SchemaStatus.CURRENT)
    with pytest.raises(FrozenInstanceError):
        state.status = SchemaStatus.INCOMPATIBLE  # type: ignore[misc]
