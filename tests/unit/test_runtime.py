"""Container entrypoint mode selection is explicit and closed."""

import pytest

from maimemo_mcp.runtime import parse_mode


def test_runtime_accepts_only_mcp_or_worker_modes() -> None:
    assert parse_mode(["mcp"]) == "mcp"
    assert parse_mode(["worker"]) == "worker"
    with pytest.raises(SystemExit):
        parse_mode(["shell"])
