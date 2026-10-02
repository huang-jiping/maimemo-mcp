"""Strict PostgreSQL CLI invocation helpers shared by backup and restore commands."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from sqlalchemy.engine import URL

from maimemo_mcp.database_url import DatabaseUrlError, parse_database_url

_CONTAINER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def database_url_from_env(name: str) -> URL:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        raise DatabaseUrlError(f"{name} must name a PostgreSQL database URL")
    url = parse_database_url(raw)
    if url.get_backend_name() != "postgresql":
        raise DatabaseUrlError(f"{name} must use PostgreSQL")
    if not url.database or url.database in {"template0", "template1"}:
        raise DatabaseUrlError(f"{name} must name a non-template database")
    return url.set(drivername="postgresql")


def resolved_new_dump_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    parent = path.parent.resolve(strict=True)
    resolved = (parent / path.name).resolve(strict=False)
    if resolved.parent != parent:
        raise ValueError("Backup output must be a direct child of an existing directory")
    if resolved.suffix != ".dump":
        raise ValueError("Backup output must end in .dump")
    if resolved.exists():
        raise FileExistsError(f"Refusing to overwrite existing backup: {resolved}")
    return resolved


def resolved_dump_input(raw: str) -> Path:
    path = Path(raw).expanduser().resolve(strict=True)
    if not path.is_file() or path.suffix != ".dump":
        raise ValueError("Backup input must be an existing regular .dump file")
    return path


@dataclass(frozen=True)
class PostgresTools:
    """Invoke native tools, or tools in a named disposable test container."""

    bin_dir: Path | None = None
    docker_container: str | None = None

    def __post_init__(self) -> None:
        if self.bin_dir is not None and self.docker_container is not None:
            raise ValueError("Choose native PostgreSQL tools or a Docker test container, not both")
        if self.docker_container is not None and not _CONTAINER.fullmatch(
            self.docker_container
        ):
            raise ValueError("Invalid Docker container name")
        if self.bin_dir is not None:
            resolved = self.bin_dir.expanduser().resolve(strict=True)
            if not resolved.is_dir():
                raise ValueError("PostgreSQL tool directory is not a directory")
            object.__setattr__(self, "bin_dir", resolved)

    def _native_tool(self, name: str) -> str:
        executable = f"{name}.exe" if os.name == "nt" else name
        if self.bin_dir is not None:
            candidate = (self.bin_dir / executable).resolve(strict=True)
            if candidate.parent != self.bin_dir or not candidate.is_file():
                raise FileNotFoundError(f"Missing PostgreSQL tool: {name}")
            return str(candidate)
        found = shutil.which(name)
        if found is None:
            raise FileNotFoundError(f"PostgreSQL tool is not on PATH: {name}")
        return found

    def command(self, name: str, *, interactive: bool = False) -> list[str]:
        if self.docker_container is None:
            return [self._native_tool(name)]
        docker = shutil.which("docker")
        if docker is None:
            raise FileNotFoundError("Docker is required for --docker-container test mode")
        # Ask Docker to inherit the named variable; never include its value in argv.
        command = [docker, "exec", "--env", "PGPASSWORD"]
        if interactive:
            command.append("-i")
        command.extend([self.docker_container, name])
        return command

    def database_argument(self, url: URL) -> tuple[str, dict[str, str]]:
        if any(key.casefold() in {"password", "sslpassword", "passfile"} for key in url.query):
            raise ValueError("Credential query parameters are not supported")
        environment = dict(os.environ)
        password = url.password
        if password is not None:
            environment["PGPASSWORD"] = password
        safe_url = URL.create(
            drivername=url.drivername,
            username=url.username,
            host=url.host,
            port=url.port,
            database=url.database,
            query=url.query,
        ).render_as_string(hide_password=False)
        return safe_url, environment

    def validate(self, *names: str) -> None:
        for name in names:
            self.run(name, ["--version"], capture_output=True)

    def run(
        self,
        name: str,
        args: list[str],
        *,
        stdin: IO[bytes] | None = None,
        stdout: IO[bytes] | int | None = None,
        capture_output: bool = False,
        text: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[Any]:
        if capture_output and stdout is not None:
            raise ValueError("capture_output and stdout are mutually exclusive")
        return subprocess.run(
            [*self.command(name, interactive=stdin is not None), *args],
            stdin=stdin,
            stdout=subprocess.PIPE if capture_output else stdout,
            stderr=subprocess.PIPE,
            text=text,
            encoding="utf-8" if text else None,
            errors="strict" if text else None,
            env=env,
            shell=False,
            check=True,
        )
