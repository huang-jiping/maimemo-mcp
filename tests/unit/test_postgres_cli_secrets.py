"""Native argv and subprocess failures must never retain database credentials."""

import importlib
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url


@pytest.mark.parametrize("password", ["SYNTHETIC_DB_SECRET", "p@ss:word/percent%"])
@pytest.mark.parametrize("docker", [False, True])
def test_postgres_url_password_is_environment_only(
    monkeypatch: pytest.MonkeyPatch, password: str, docker: bool,
) -> None:
    from sqlalchemy.engine import URL
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "scripts"))
    tools = importlib.import_module("_postgres_cli").PostgresTools(
        docker_container="synthetic-container" if docker else None
    )
    url = URL.create("postgresql", username="u", password=password,
                     host="h.invalid", database="d", query={"sslmode": "require"})
    dsn, environment = tools.database_argument(url)
    assert make_url(dsn).password is None
    assert make_url(dsn).query == {"sslmode": "require"}
    assert environment["PGPASSWORD"] == password
    monkeypatch.setattr(type(tools), "command", lambda self, name, **kw: [sys.executable])
    with pytest.raises(subprocess.CalledProcessError) as caught:
        tools.run("psql", ["-c", "import sys; sys.exit(1)", dsn], env=environment)
    assert password not in str(caught.value)
    assert password not in repr(caught.value.cmd)


@pytest.mark.parametrize("key", ["password", "sslpassword", "passfile", "PASSWORD"])
def test_query_credentials_are_rejected_without_echo(
    monkeypatch: pytest.MonkeyPatch, key: str,
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "scripts"))
    tools = importlib.import_module("_postgres_cli").PostgresTools()
    url = make_url(f"postgresql://u@h.invalid/d?{key}=SYNTHETIC_QUERY_SECRET")
    with pytest.raises(ValueError) as caught:
        tools.database_argument(url)
    assert "SYNTHETIC_QUERY_SECRET" not in str(caught.value)
