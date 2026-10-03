"""Independent runtime entrypoints reject legacy modes and redact configuration."""

import pytest


def test_mcp_runtime_rejects_worker_mode() -> None:
    from maimemo_mcp.runtime import main

    with pytest.raises(SystemExit):
        main(["worker"])


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
    assert main([]) == 2
    output = capsys.readouterr()
    assert "SYNTHETIC_STARTUP_SECRET" not in output.err + output.out
    assert "postgresql://" not in output.err + output.out
    assert "configuration_error" in output.err


@pytest.mark.parametrize("private_url", [
    "postgresql+psycopg://u:prefix@host:LEAK@localhost/d",
    "postgresql+psycopg://u:prefix@host/d?port=LEAK@localhost/d",
    "postgresql+psycopg://u:prefix@LEAK@localhost/d",
])
def test_malformed_database_url_fails_before_runtime_without_secret_or_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
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

    assert main([]) == 2

    output = capsys.readouterr()
    combined = output.err + output.out
    assert combined == "configuration_error database_url:invalid\n"
    assert "LEAK" not in combined
    assert private_url not in combined
    assert "Traceback" not in combined


def test_runtime_preflight_controls_late_engine_url_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from maimemo.config import CoreSettings, DatabaseSettings, UpstreamCredentialSettings
    from maimemo_mcp.config import MCPSettings
    from maimemo_mcp.runtime import main

    settings = MCPSettings.model_construct(
        core=CoreSettings.model_construct(
            database=DatabaseSettings.model_construct(
                database_url=(
                    "postgresql+psycopg://u:prefix@host/d?port=LEAK@localhost/d"
                )
            )
        ),
        upstream=UpstreamCredentialSettings.model_construct(),
    )
    monkeypatch.setattr(MCPSettings, "load", lambda: settings)

    assert main([]) == 2

    output = capsys.readouterr()
    combined = output.err + output.out
    assert combined == "configuration_error database_url:invalid\n"
    assert "LEAK" not in combined
    assert "Traceback" not in combined


@pytest.mark.parametrize("invalid", [False, True])
def test_worker_startup_configuration_failure_is_controlled_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    invalid: bool,
) -> None:
    import os

    from maimemo_worker.runtime import main as worker_main

    for name in list(os.environ):
        if name.startswith("MAIMEMO_"):
            monkeypatch.delenv(name)
    url = "postgresql://u:SYNTHETIC_WORKER_SECRET@h.invalid/d"
    monkeypatch.setenv("MAIMEMO_DATABASE_URL", url)
    if invalid:
        monkeypatch.setenv("MAIMEMO_TOKEN_FILE", "unused")
        monkeypatch.setenv("MAIMEMO_TOKEN_FINGERPRINT_KEY_FILE", "unused")
        monkeypatch.setenv("MAIMEMO_TODAY_INTERVAL_MINUTES", url)

    assert worker_main([]) == 2

    output = capsys.readouterr()
    combined = output.err + output.out
    assert "SYNTHETIC_WORKER_SECRET" not in combined
    assert "postgresql://" not in combined
    assert "configuration_error" in output.err
