"""Migration CLI is closed to arbitrary Alembic commands and secrets."""

import pytest


def test_current_and_upgrade_head_use_only_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from maimemo_server import migrate

    calls: list[tuple[str, str | None]] = []
    monkeypatch.setenv("MAIMEMO_DATABASE_URL", "postgresql+psycopg://u:p@db.invalid/maimemo")
    monkeypatch.setenv("MAIMEMO_TOKEN_FILE", "must-not-be-read")
    monkeypatch.setattr(migrate.command, "current", lambda config: calls.append(("current", None)))
    monkeypatch.setattr(
        migrate,
        "run_upgrade",
        lambda settings: calls.append(("upgrade", "head")),
    )

    assert migrate.main(["current"]) == 0
    assert migrate.main(["upgrade", "head"]) == 0
    assert calls == [("current", None), ("upgrade", "head")]


def test_missing_database_url_is_controlled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from maimemo_server.migrate import main

    monkeypatch.delenv("MAIMEMO_DATABASE_URL", raising=False)

    assert main(["current"]) == 2
    assert capsys.readouterr().err == "configuration_error database_url:missing\n"


@pytest.mark.parametrize(
    "argv",
    [
        ["downgrade", "base"],
        ["upgrade", "0004"],
        ["upgrade", "head", "extra"],
        ["current", "extra"],
    ],
)
def test_unsupported_migration_commands_do_not_execute(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    from maimemo_server import migrate

    monkeypatch.setenv("MAIMEMO_DATABASE_URL", "postgresql+psycopg://u:p@db.invalid/maimemo")
    monkeypatch.setattr(
        migrate.command,
        "current",
        lambda config: pytest.fail("unsupported command executed current"),
    )
    monkeypatch.setattr(
        migrate.command,
        "upgrade",
        lambda config, revision: pytest.fail("unsupported command executed upgrade"),
    )

    with pytest.raises(SystemExit):
        migrate.main(argv)
