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
