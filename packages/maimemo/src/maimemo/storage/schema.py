"""Read-only, exact migration compatibility checks with safe failure reasons."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class SchemaDefinitionError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("schema_definition_invalid")


class SchemaNotReadyError(RuntimeError):
    def __init__(self, *, unavailable: bool = False) -> None:
        super().__init__("database_unavailable" if unavailable else "schema_incompatible")


class SchemaStatus(StrEnum):
    CURRENT = "current"
    EMPTY = "empty"
    INCOMPATIBLE = "incompatible"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SchemaState:
    alembic_revisions: tuple[str, ...]
    metadata_revisions: tuple[str, ...]
    expected_revision: str
    status: SchemaStatus


def expected_schema_revision(config_path: Path = Path("alembic.ini")) -> str:
    """Resolve the shipped head independently of the process working directory."""
    if config_path == Path("alembic.ini"):
        candidates = (Path.cwd() / "alembic.ini",) + tuple(
            parent / "alembic.ini" for parent in Path(__file__).resolve().parents
        )
        config_path = next(
            (candidate for candidate in candidates if candidate.is_file()),
            config_path,
        )
    try:
        heads = ScriptDirectory.from_config(Config(str(config_path))).get_heads()
        if len(heads) != 1:
            raise SchemaDefinitionError()
        return heads[0]
    except Exception:
        # Neither filesystem paths nor configuration/driver details cross this boundary.
        raise SchemaDefinitionError() from None


async def inspect_schema(engine: AsyncEngine, expected: str) -> SchemaState:
    """Both version tables must contain exactly the expected single revision."""
    alembic: tuple[str, ...] = ()
    metadata: tuple[str, ...] = ()
    try:
        async with engine.connect() as connection:
            tables = (
                await connection.execute(
                    text("SELECT to_regclass('alembic_version'), to_regclass('schema_metadata')")
                )
            ).one()
            if tables[0] is not None:
                alembic = tuple(
                    (
                        await connection.execute(
                            text("SELECT version_num FROM alembic_version ORDER BY version_num")
                        )
                    ).scalars().all()
                )
            if tables[1] is not None:
                metadata = tuple(
                    (
                        await connection.execute(
                            text(
                                "SELECT schema_version FROM schema_metadata ORDER BY schema_version"
                            )
                        )
                    ).scalars().all()
                )
    except Exception:
        return SchemaState(alembic, metadata, expected, SchemaStatus.UNAVAILABLE)
    if tables[0] is not None and tables[1] is not None and alembic == metadata == (expected,):
        status = SchemaStatus.CURRENT
    elif (tables[0] is None and tables[1] is None) or (
        tables[0] is not None and tables[1] is not None and not alembic and not metadata
    ):
        status = SchemaStatus.EMPTY
    else:
        status = SchemaStatus.INCOMPATIBLE
    return SchemaState(alembic, metadata, expected, status)


async def require_current_schema(engine: AsyncEngine, expected: str) -> None:
    state = await inspect_schema(engine, expected)
    if state.status is not SchemaStatus.CURRENT:
        raise SchemaNotReadyError(unavailable=state.status is SchemaStatus.UNAVAILABLE)
