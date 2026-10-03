"""Compose contract for the four-service NAS deployment."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def compose_config() -> dict[str, object]:
    environment = dict(os.environ)
    environment.update(
        {
            "MAIMEMO_DATABASE_URL": (
                "postgresql+psycopg://maimemo:synthetic@postgres.invalid:5432/maimemo"
            ),
        }
    )
    completed = subprocess.run(
        ["docker", "compose", "-f", "compose.yaml", "config", "--format", "json"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def mounted_secret_names(service: dict[str, object]) -> set[str]:
    mounts = service.get("secrets", [])
    assert isinstance(mounts, list)
    return {mount["source"] for mount in mounts if isinstance(mount, dict)}


def test_compose_has_final_project_services_and_outbound_network() -> None:
    config = compose_config()
    services = config["services"]
    networks = config["networks"]

    assert config["name"] == "maimemo"
    assert isinstance(services, dict)
    assert set(services) == {"migrate", "server", "mcp", "worker"}
    assert isinstance(networks, dict)
    assert networks["maimemo-app"].get("internal") is not True
    assert all("maimemo-app" in service["networks"] for service in services.values())


def test_compose_uses_distinct_images_and_private_runtime_ports() -> None:
    services = compose_config()["services"]
    assert isinstance(services, dict)

    assert services["server"]["image"] == "ghcr.io/huang-jiping/maimemo-server:0.2.0"
    assert services["migrate"]["image"] == services["server"]["image"]
    assert services["mcp"]["image"] == "ghcr.io/huang-jiping/maimemo-mcp:0.2.0"
    assert services["worker"]["image"] == "ghcr.io/huang-jiping/maimemo-worker:0.2.0"
    assert "ports" not in services["mcp"]
    assert "ports" not in services["worker"]
    assert services["server"]["expose"] == ["8080"]
    assert services["mcp"]["expose"] == ["8000"]


def test_compose_keeps_upstream_secrets_out_of_server_and_migrations() -> None:
    services = compose_config()["services"]
    assert isinstance(services, dict)

    expected = {"maimemo_token", "token_fingerprint_key"}
    assert mounted_secret_names(services["mcp"]) == expected
    assert mounted_secret_names(services["worker"]) == expected
    assert mounted_secret_names(services["server"]) == set()
    assert mounted_secret_names(services["migrate"]) == set()
    assert services["migrate"]["entrypoint"] == ["maimemo-migrate"]
    assert services["migrate"]["restart"] == "no"
