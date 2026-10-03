"""Restricted Alembic entrypoint for current revision and forward migration."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from alembic import command
from alembic.config import Config
from pydantic import ValidationError

from maimemo_server.config import MigrationSettings
from maimemo_server.migration_runner import MigrationError, run_upgrade


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("current", help="show the current database revision")
    upgrade = commands.add_parser("upgrade", help="upgrade the database to head")
    upgrade.add_argument("revision", choices=("head",))
    return parser.parse_args(argv)


def _alembic_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_arguments(argv)
    try:
        settings = MigrationSettings.load()
    except ValidationError as exc:
        missing = any(error["type"] == "missing" for error in exc.errors())
        category = "missing" if missing else "invalid"
        print(f"configuration_error database_url:{category}", file=sys.stderr)
        return 2
    config = _alembic_config(settings.database.database_url)
    if args.command == "current":
        command.current(config)
    else:
        try:
            run_upgrade(settings)
        except MigrationError as exc:
            print(f"migration_error {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
