"""Restore a custom dump only into a new, explicitly disposable PostgreSQL database."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from _postgres_cli import PostgresTools, database_url_from_env, resolved_dump_input
from maimemo.database_url import DatabaseUrlError
from sqlalchemy.engine import URL

# PostgreSQL identifiers are at most 63 bytes.  The fixed ASCII prefix is 16
# bytes, so cap the suffix at 47 rather than relying on server-side truncation.
_DISPOSABLE_DATABASE = re.compile(r"^maimemo_restore_[a-z0-9_]{8,47}$")
_EXPECTED_ALEMBIC_REVISION = "0004"
_CRITICAL_TABLES = (
    "alembic_version",
    "schema_metadata",
    "ingestion_run",
    "api_snapshot",
    "failed_api_snapshot",
    "vocabulary",
    "daily_progress",
    "daily_word_observation",
    "study_record_snapshot",
    "weakness_score",
    "learning_feedback_event",
    "api_rate_limit_window",
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--backup", required=True)
    result.add_argument("--target-database", required=True)
    result.add_argument("--confirm-disposable-target", required=True)
    result.add_argument(
        "--admin-url-env",
        default="MAIMEMO_RESTORE_ADMIN_URL",
        help="Environment variable containing an admin URL to a maintenance database",
    )
    result.add_argument("--pg-bin-dir", type=Path)
    result.add_argument(
        "--docker-container",
        help="Disposable integration-test container; use native tools in production",
    )
    return result


def _validate_target(target: str, confirmation: str, admin_database: str) -> None:
    if target != confirmation:
        raise ValueError("Disposable target confirmation must exactly match target database")
    if not _DISPOSABLE_DATABASE.fullmatch(target):
        raise ValueError("Target must match maimemo_restore_[a-z0-9_]{8,47}")
    if target == admin_database:
        raise ValueError("Restore target cannot be the maintenance database")


def _query(
    tools: PostgresTools,
    database_url: URL,
    sql: str,
) -> list[str]:
    dsn, environment = tools.database_argument(database_url)
    result = tools.run(
        "psql",
        [
            "--no-psqlrc",
            "--set",
            "ON_ERROR_STOP=1",
            "--tuples-only",
            "--no-align",
            "--dbname",
            dsn,
            "--command",
            sql,
        ],
        capture_output=True,
        text=True,
        env=environment,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _validate_restored_database(tools: PostgresTools, database_url: URL) -> None:
    tables = ",".join(f"'{table}'" for table in _CRITICAL_TABLES)
    sql = f"""
SELECT count(*) = {len(_CRITICAL_TABLES)}
FROM information_schema.tables
WHERE table_schema = 'public' AND table_name IN ({tables});
SELECT count(*) = 0 FROM api_snapshot
WHERE length(request_hash) = 0 OR length(content_hash) = 0;
SELECT count(*) = 0 FROM failed_api_snapshot
WHERE length(request_hash) = 0 OR length(content_hash) = 0;
SELECT count(*) = 0
FROM learning_feedback_event child
LEFT JOIN learning_feedback_event parent ON parent.id = child.retracted_event_id
WHERE child.event_type = 'RETRACTION' AND parent.id IS NULL;
SELECT (SELECT version_num FROM alembic_version) = '{_EXPECTED_ALEMBIC_REVISION}'
   AND (SELECT schema_version FROM schema_metadata ORDER BY installed_at DESC LIMIT 1)
       = '{_EXPECTED_ALEMBIC_REVISION}';
"""
    if _query(tools, database_url, sql) != ["t", "t", "t", "t", "t"]:
        raise RuntimeError("Restored database failed critical integrity validation")


def _restore_new_database(
    tools: PostgresTools,
    admin_url: URL,
    target: str,
    backup: Path,
) -> None:
    admin_dsn, admin_environment = tools.database_argument(admin_url)
    owner_arguments = ["--owner", admin_url.username] if admin_url.username else []
    created = False
    try:
        tools.run(
            "createdb",
            ["--maintenance-db", admin_dsn, *owner_arguments, target],
            env=admin_environment,
        )
        created = True
        target_url = admin_url.set(database=target)
        target_dsn, target_environment = tools.database_argument(target_url)
        with backup.open("rb") as stream:
            tools.run(
                "pg_restore",
                [
                    "--exit-on-error",
                    "--no-owner",
                    "--no-privileges",
                    "--dbname",
                    target_dsn,
                ],
                stdin=stream,
                env=target_environment,
            )
        _validate_restored_database(tools, target_url)
    except BaseException as primary:
        if created:
            try:
                tools.run(
                    "dropdb",
                    ["--maintenance-db", admin_dsn, "--force", "--if-exists", target],
                    env=admin_environment,
                )
            except BaseException as cleanup_error:
                primary.add_note(
                    "Disposable restore cleanup failed with "
                    f"{type(cleanup_error).__name__}; manual cleanup is required"
                )
        raise


def _run(args: argparse.Namespace) -> int:
    backup = resolved_dump_input(args.backup)
    admin_url = database_url_from_env(args.admin_url_env)
    assert admin_url.database is not None
    _validate_target(args.target_database, args.confirm_disposable_target, admin_url.database)
    tools = PostgresTools(args.pg_bin_dir, args.docker_container)
    tools.validate("psql", "createdb", "pg_restore", "dropdb")

    escaped_target = args.target_database.replace("'", "''")
    exists = _query(
        tools,
        admin_url,
        f"SELECT 1 FROM pg_database WHERE datname = '{escaped_target}';",
    )
    if exists:
        raise RuntimeError("Refusing to restore into an existing database")

    _restore_new_database(tools, admin_url, args.target_database, backup)
    print(f"Restore validated in disposable database: {args.target_database}")
    return 0


def main() -> int:
    args = parser().parse_args()
    try:
        return _run(args)
    except DatabaseUrlError:
        print("configuration_error database_url:invalid", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
