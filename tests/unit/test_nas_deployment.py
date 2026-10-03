"""NAS delivery preserves the four-service boundary and private access model."""

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[2]
NAS = ROOT / "deploy" / "nas"


def nas_compose() -> dict[str, Any]:
    return yaml.safe_load((NAS / "compose.yaml").read_text(encoding="utf-8"))


def test_nas_guide_keeps_engine_and_lan_isolation_gates() -> None:
    guide = (ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
    for required in (
        "docker version",
        ">=28.0.0",
        "厂商明确回补",
        "两项均不满足时禁止启动应用",
        "NAS_IP 8000",
        "HTTP 403",
        "不能替代",
    ):
        assert required in guide


def test_nas_compose_has_final_project_services_and_network() -> None:
    compose = nas_compose()
    assert compose["name"] == "maimemo"
    assert set(compose["services"]) == {"migrate", "server", "mcp", "worker"}
    assert compose["networks"] == {"db_net": {"external": True, "name": "db_net"}}
    for service in compose["services"].values():
        assert service["networks"] == ["db_net"]
        assert service["user"] == "10001:10001"
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["mem_limit"] == "512m"
        assert service["pids_limit"] == 128


def test_nas_uses_three_images_and_isolates_upstream_secrets() -> None:
    services = nas_compose()["services"]
    assert services["server"]["image"].startswith("${MAIMEMO_SERVER_IMAGE:")
    assert services["migrate"]["image"] == services["server"]["image"]
    assert services["mcp"]["image"].startswith("${MAIMEMO_MCP_IMAGE:")
    assert services["worker"]["image"].startswith("${MAIMEMO_WORKER_IMAGE:")
    assert "secrets" not in services["server"]
    assert "secrets" not in services["migrate"]
    assert services["mcp"]["secrets"] == services["worker"]["secrets"]
    assert services["mcp"]["ports"] == ["127.0.0.1:8000:8000"]


def test_worker_writes_drift_state_while_mcp_mount_is_read_only() -> None:
    services = nas_compose()["services"]
    assert services["mcp"]["volumes"][0]["read_only"] is True
    assert services["worker"]["volumes"][0]["read_only"] is False
    assert services["worker"]["environment"]["MAIMEMO_OPENAPI_PINNED_FILE"] == (
        "/app/openapi/maimemo-api.yaml"
    )


def test_nas_examples_contain_no_secret_values_and_pin_three_digests() -> None:
    values = dict(
        line.split("=", 1)
        for line in (NAS / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    assert all(
        values[name].startswith(f"ghcr.io/huang-jiping/{image}@sha256:")
        for name, image in (
            ("MAIMEMO_SERVER_IMAGE", "maimemo-server"),
            ("MAIMEMO_MCP_IMAGE", "maimemo-mcp"),
            ("MAIMEMO_WORKER_IMAGE", "maimemo-worker"),
        )
    )
    sources = [NAS / "compose.yaml", NAS / ".env.example", ROOT / "DEPLOYMENT.md"]
    for source in sources:
        text = source.read_text(encoding="utf-8")
        assert not re.search(r"\b(?:ghp_|github_pat_|sk-proj-)[A-Za-z0-9_]{16,}", text)
        assert not re.search(r"(?m)^\s*MAIMEMO_TOKEN\s*=\s*\S+", text)
