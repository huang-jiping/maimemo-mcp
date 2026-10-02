"""Native argv and subprocess failures must never retain database credentials."""

import importlib
import os
import subprocess
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

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


@pytest.mark.parametrize("docker", [False, True])
def test_postgres_argv_allows_only_bounded_sslmode_query(
    monkeypatch: pytest.MonkeyPatch, docker: bool,
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "scripts"))
    tools = importlib.import_module("_postgres_cli").PostgresTools(
        docker_container="synthetic-container" if docker else None
    )
    safe_url = make_url("postgresql://u:p@h.invalid/d?sslmode=require")
    dsn, environment = tools.database_argument(safe_url)
    assert make_url(dsn).query == {"sslmode": "require"}
    assert environment["PGPASSWORD"] == "p"

    unsafe_url = make_url("postgresql://u:p@h.invalid/d?application_name=REVIEW_SECRET")
    with pytest.raises(ValueError) as caught:
        tools.database_argument(unsafe_url)
    formatted = "".join(traceback.format_exception(caught.value))
    assert "REVIEW_SECRET" not in formatted
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_malformed_database_url_error_chain_does_not_retain_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "scripts"))
    postgres_cli = importlib.import_module("_postgres_cli")
    private_url = "postgresql://u:prefix@host:LEAK@localhost/d"
    monkeypatch.setenv("MAIMEMO_BACKUP_DATABASE_URL", private_url)

    with pytest.raises(ValueError) as caught:
        postgres_cli.database_url_from_env("MAIMEMO_BACKUP_DATABASE_URL")

    formatted = "".join(traceback.format_exception(caught.value))
    assert "LEAK" not in formatted
    assert private_url not in formatted
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    ("script", "url_name", "arguments"),
    [
        (
            "backup_postgres.py",
            "MAIMEMO_BACKUP_DATABASE_URL",
            lambda tmp_path: ["--output", str(tmp_path / "new.dump")],
        ),
        (
            "restore_postgres.py",
            "MAIMEMO_RESTORE_ADMIN_URL",
            lambda tmp_path: [
                "--backup",
                str(tmp_path / "synthetic.dump"),
                "--target-database",
                "maimemo_restore_12345678",
                "--confirm-disposable-target",
                "maimemo_restore_12345678",
            ],
        ),
    ],
)
def test_backup_restore_entrypoints_reject_malformed_url_without_secret_or_traceback(
    tmp_path: Path,
    script: str,
    url_name: str,
    arguments: Callable[[Path], list[str]],
) -> None:
    (tmp_path / "synthetic.dump").write_bytes(b"synthetic")
    private_url = "postgresql://u:prefix@host:LEAK@localhost/d"
    environment = dict(os.environ)
    environment[url_name] = private_url

    result = subprocess.run(
        [sys.executable, str(Path(__file__).parents[2] / "scripts" / script), *arguments(tmp_path)],
        cwd=Path(__file__).parents[2],
        env=environment,
        text=True,
        capture_output=True,
        shell=False,
        check=False,
    )

    combined = result.stderr + result.stdout
    assert result.returncode == 2
    assert combined == "configuration_error database_url:invalid\n"
    assert "LEAK" not in combined
    assert private_url not in combined
    assert "Traceback" not in combined


@pytest.mark.parametrize(
    ("module_name", "url_name"),
    [
        ("backup_postgres", "MAIMEMO_BACKUP_DATABASE_URL"),
        ("restore_postgres", "MAIMEMO_RESTORE_ADMIN_URL"),
    ],
)
def test_backup_restore_main_rejects_parseable_query_override_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    module_name: str,
    url_name: str,
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "scripts"))
    module = importlib.import_module(module_name)
    private_url = "postgresql://u:prefix@host/d?port=REVIEW_SECRET@localhost/d"
    monkeypatch.setenv(url_name, private_url)
    backup = tmp_path / "synthetic.dump"
    backup.write_bytes(b"synthetic")
    arguments = SimpleNamespace(
        output=tmp_path / "new.dump",
        database_url_env=url_name,
        backup=backup,
        target_database="maimemo_restore_12345678",
        confirm_disposable_target="maimemo_restore_12345678",
        admin_url_env=url_name,
        pg_bin_dir=None,
        docker_container=None,
    )
    monkeypatch.setattr(module, "parser", lambda: SimpleNamespace(parse_args=lambda: arguments))
    if module_name == "backup_postgres":
        monkeypatch.setattr(module, "resolved_new_dump_path", lambda value: Path(value))
    else:
        monkeypatch.setattr(module, "resolved_dump_input", lambda value: Path(value))
    monkeypatch.setattr(module.PostgresTools, "validate", lambda self, *names: None)

    def fail_if_started(self: object, name: str, args: list[str], **kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, [name, *args])

    monkeypatch.setattr(module.PostgresTools, "run", fail_if_started)

    try:
        result = module.main()
    except subprocess.CalledProcessError as exc:
        assert "REVIEW_SECRET" in repr(exc.cmd)
        pytest.fail("database query value reached subprocess argv")

    output = capsys.readouterr()
    combined = output.err + output.out
    assert result == 2
    assert combined == "configuration_error database_url:invalid\n"
    assert "REVIEW_SECRET" not in combined
    assert "Traceback" not in combined
