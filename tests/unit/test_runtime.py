"""Container entrypoint mode selection is explicit and closed."""

import pytest

from maimemo_mcp.runtime import parse_mode


def test_runtime_accepts_only_mcp_or_worker_modes() -> None:
    assert parse_mode(["mcp"]) == "mcp"
    assert parse_mode(["worker"]) == "worker"
    with pytest.raises(SystemExit):
        parse_mode(["shell"])


@pytest.mark.parametrize("invalid", [False, True])
def test_startup_configuration_failure_is_controlled_and_secret_free(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], invalid: bool,
) -> None:
    import os

    from maimemo_mcp.runtime import main

    for name in list(os.environ):
        if name.startswith("MAIMEMO_"):
            monkeypatch.delenv(name)
    url = "postgresql://u:SYNTHETIC_STARTUP_SECRET@h.invalid/d"
    monkeypatch.setenv("MAIMEMO_DATABASE_URL", url)
    if invalid:
        monkeypatch.setenv("MAIMEMO_TOKEN_FILE", "unused")
        monkeypatch.setenv("MAIMEMO_TOKEN_FINGERPRINT_KEY_FILE", "unused")
        monkeypatch.setenv("MAIMEMO_MCP_PORT", url)
    assert main(["worker"]) == 2
    output = capsys.readouterr()
    assert "SYNTHETIC_STARTUP_SECRET" not in output.err + output.out
    assert "postgresql://" not in output.err + output.out
    assert "configuration_error" in output.err


@pytest.mark.parametrize("mode", ["worker", "mcp"])
@pytest.mark.parametrize("private_url", [
    "postgresql+psycopg://u:prefix@host:LEAK@localhost/d",
    "postgresql+psycopg://u:prefix@host/d?port=LEAK@localhost/d",
    "postgresql+psycopg://u:prefix@LEAK@localhost/d",
])
def test_malformed_database_url_fails_before_runtime_without_secret_or_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
    private_url: str,
) -> None:
    import os

    from maimemo_mcp.runtime import main

    for name in list(os.environ):
        if name.startswith("MAIMEMO_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("MAIMEMO_DATABASE_URL", private_url)
    monkeypatch.setenv("MAIMEMO_TOKEN_FILE", "unused")
    monkeypatch.setenv("MAIMEMO_TOKEN_FINGERPRINT_KEY_FILE", "unused")

    assert main([mode]) == 2

    output = capsys.readouterr()
    combined = output.err + output.out
    assert combined == "configuration_error database_url:invalid\n"
    assert "LEAK" not in combined
    assert private_url not in combined
    assert "Traceback" not in combined


@pytest.mark.parametrize("mode", ["worker", "mcp"])
def test_runtime_preflight_controls_late_engine_url_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    from pathlib import Path

    from maimemo_mcp.config import Settings
    from maimemo_mcp.runtime import main

    settings = Settings.model_construct(
        database_url=(
            "postgresql+psycopg://u:prefix@host/d?port=LEAK@localhost/d"
        ),
        token_file=Path("unused"),
        token_fingerprint_key_file=Path("unused"),
    )
    monkeypatch.setattr(Settings, "load", lambda: settings)

    assert main([mode]) == 2

    output = capsys.readouterr()
    combined = output.err + output.out
    assert combined == "configuration_error database_url:invalid\n"
    assert "LEAK" not in combined
    assert "Traceback" not in combined
