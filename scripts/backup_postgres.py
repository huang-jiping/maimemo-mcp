"""Create a non-overwriting custom-format PostgreSQL backup."""

from __future__ import annotations

import argparse
from pathlib import Path

from _postgres_cli import PostgresTools, database_url_from_env, resolved_new_dump_path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--output", required=True, help="New .dump file; never overwritten")
    result.add_argument(
        "--database-url-env",
        default="MAIMEMO_BACKUP_DATABASE_URL",
        help="Environment variable containing the source PostgreSQL URL",
    )
    result.add_argument("--pg-bin-dir", type=Path)
    result.add_argument(
        "--docker-container",
        help="Disposable integration-test container; use native tools in production",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    output = resolved_new_dump_path(args.output)
    database_url = database_url_from_env(args.database_url_env)
    tools = PostgresTools(args.pg_bin_dir, args.docker_container)
    tools.validate("pg_dump")
    dsn, environment = tools.database_argument(database_url)
    created = False
    try:
        with output.open("xb") as stream:
            created = True
            tools.run(
                "pg_dump",
                ["--format=custom", "--no-owner", "--no-privileges", "--dbname", dsn],
                stdout=stream,
                env=environment,
            )
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise
    print(f"Backup created: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
