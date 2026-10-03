"""Private NAS deployment must preserve isolation and load valid runtime settings."""

import importlib.util
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from maimemo_mcp.config import Settings

ROOT = Path(__file__).parents[2]
NAS = ROOT / "deploy" / "nas"


def test_nas_guide_gates_localhost_isolation_on_engine_fix_and_lan_verification() -> None:
    guide = (ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
    prerequisites = guide.split("## 目录与前置条件", 1)[1].split("## 配置与 secret 权限", 1)[0]
    for requirement in (
        "docker version", "Server", "Engine", ">=28.0.0", "厂商明确回补",
        "两项均不满足时禁止启动应用", "禁止连接 Tunnel",
        "https://docs.docker.com/engine/network/port-publishing/",
    ):
        assert requirement in prerequisites, f"Missing deployment gate: {requirement}"
    assert "## UGOS 首次部署与检查" in guide, "Missing UGOS deployment acceptance flow"
    acceptance = guide.split("## UGOS 首次部署与检查", 1)[1].split("## Tunnel", 1)[0]
    for requirement in (
        "另一台同一 LAN", "NAS_IP:8000", "不可达", "nc -vz -w 3 NAS_IP 8000",
        "不能将 HTTP 403", "不得连接 Tunnel", "不能替代版本/回补门禁",
        "不覆盖旧版 localhost 发布漏洞",
    ):
        assert requirement in acceptance, f"Missing isolation acceptance: {requirement}"
    operations = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
    for requirement in (
        "docker version", ">=28.0.0", "厂商明确回补", "NAS_IP:8000",
        "两项均不满足时禁止启动应用", "不能替代版本/回补门禁",
    ):
        assert requirement in operations, f"Operations guide omits gate: {requirement}"
    for document in (guide, operations):
        assert "等效" not in document, "Unpatched Engine must not have an isolation exception"


def nas_compose() -> dict[str, Any]:
    assert (NAS / "compose.yaml").is_file(), "NAS deployment template is missing"
    return yaml.safe_load((NAS / "compose.yaml").read_text(encoding="utf-8"))


def test_nas_connects_only_existing_database_network_and_loopback() -> None:
    compose = nas_compose()
    assert compose["name"] == "maimemo-mcp"
    assert set(compose["services"]) == {"maimemo-mcp", "maimemo-worker"}
    assert compose["networks"] == {"db_net": {"external": True, "name": "db_net"}}
    for name, service in compose["services"].items():
        assert service["container_name"] == name
        assert service["networks"] == ["db_net"]
        assert "network_mode" not in service
        assert "privileged" not in service
    assert compose["services"]["maimemo-mcp"]["ports"] == ["127.0.0.1:8000:8000"]
    assert compose["services"]["maimemo-mcp"]["expose"] == ["8000"]
    assert "ports" not in compose["services"]["maimemo-worker"]
    assert compose["services"]["maimemo-worker"]["command"] == ["worker"]


def test_nas_pulls_shared_release_and_retains_runtime_hardening() -> None:
    for service in nas_compose()["services"].values():
        assert "build" not in service
        assert service["image"] == "ghcr.io/huang-jiping/maimemo-mcp:${IMAGE_TAG:-stable}"
        assert service["pull_policy"] == "always"
        assert service["user"] == "1000:10"
        assert 0 < float(service["cpus"]) <= 2
        assert service["mem_limit"] == "512m"
        assert 0 < service["pids_limit"] <= 256
        assert service["restart"] == "unless-stopped"
        assert service["init"] is True
        assert service["env_file"] == [".env"]
        assert service["read_only"] is True
        assert service["tmpfs"] == ["/tmp:size=16m,mode=1777"]
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["logging"] == {
            "driver": "json-file", "options": {"max-size": "10m", "max-file": "3"},
        }
        assert service["secrets"] == ["maimemo_token", "token_fingerprint_key"]
    compose = nas_compose()
    assert compose["secrets"] == {
        "maimemo_token": {"file": "./secrets/maimemo_token"},
        "token_fingerprint_key": {"file": "./secrets/token_fingerprint_key"},
    }
    check = compose["services"]["maimemo-mcp"]["healthcheck"]
    assert "/health/ready" in check["test"][-1]
    assert "127.0.0.1:8000" in check["test"][-1]
    assert check["start_period"] == "6m"
    assert compose["services"]["maimemo-worker"]["depends_on"] == {
        "maimemo-mcp": {"condition": "service_healthy"},
    }
    assert "healthcheck" not in compose["services"]["maimemo-worker"]


def test_nas_worker_can_publish_drift_state_and_mcp_cannot_modify_it() -> None:
    services = nas_compose()["services"]
    for name, readonly in (("maimemo-mcp", True), ("maimemo-worker", False)):
        assert services[name]["volumes"] == [{
            "type": "bind", "source": "./data", "target": "/var/lib/maimemo",
            "read_only": readonly,
        }]
    assert services["maimemo-mcp"]["environment"] == services["maimemo-worker"]["environment"]


def test_nas_example_loads_settings_without_embedded_credentials() -> None:
    assert (NAS / ".env.example").is_file(), "NAS environment example is missing"
    values = dict(
        line.split("=", 1) for line in (NAS / ".env.example").read_text(encoding="utf-8")
        .splitlines() if line and not line.startswith("#")
    )
    settings = Settings.load(values)
    assert settings.database_url == (
        "postgresql+psycopg://maimemo:REPLACE_WITH_URL_ENCODED_PASSWORD"
        "@replace-with-db-net-dns.invalid:5432/maimemo?sslmode=disable"
    )
    assert values["IMAGE_TAG"] == "stable"
    assert settings.token_file == Path("/run/secrets/maimemo_token")
    assert settings.token_fingerprint_key_file == Path("/run/secrets/token_fingerprint_key")
    assert settings.openapi_drift_state_file == Path("/var/lib/maimemo/openapi-drift.json")
    assert settings.mcp_host == "0.0.0.0"
    assert settings.mcp_port == 8000
    assert "MAIMEMO_TOKEN" not in values
    assert "TZ" in nas_compose()["services"]["maimemo-mcp"]["environment"]


@pytest.mark.parametrize("document", ["DEPLOYMENT.md", "docs/operations.md"])
def test_nas_guides_cover_ugos_lifecycle_backup_and_readonly_secret_checks(document: str) -> None:
    guide = (ROOT / document).read_text(encoding="utf-8")
    for requirement in (
        "UGOS Pro", "Docker → 项目", "创建/导入", "deploy/nas/compose.yaml",
        "ghcr.io/huang-jiping/maimemo-mcp", "IMAGE_TAG", "stable", "vX.Y.Z", "digest",
        "2 / 2", "拉取", "重建", "自动迁移", "上一", "破坏性", "停止整个项目",
        "pgAdmin", "整个 `maimemo` 数据库", "恢复演练", "alembic_version", "schema_metadata",
        "UID 1000 / GID 10", "0400", "ACL", "只读", "六小时", "0644", "0700",
        "真实 Docker DNS", "本地 `.env`", "fingerprint key", "未验证", "已发布",
        "v0.1.0", "sha256:2fc31dcab9d514d13b1abf3da499ae1ddd64e8d7e92a72743b4f18f1bdbf0680",
    ):
        assert requirement in guide, f"{document} missing deployment contract: {requirement}"
    assert "未发布" not in guide


@pytest.mark.parametrize("document", ["DEPLOYMENT.md", "docs/operations.md", "README.md"])
def test_nas_production_instructions_do_not_depend_on_source_or_host_scripts(document: str) -> None:
    guide = (ROOT / document).read_text(encoding="utf-8")
    if document == "README.md":
        guide = guide.split("NAS 私有部署", 1)[1].split("## 只读真实接口冒烟", 1)[0]
    elif document == "docs/operations.md":
        guide = guide.split("## 7.", 1)[0]
    for forbidden in (
        r"git\s+clone", r"app/", r"docker\s+compose\s+build", r"local/maimemo-mcp",
        r"\buv\s+(?:run|sync)", r"scripts/", r"-m\s+alembic\s+upgrade",
    ):
        assert not re.search(forbidden, guide), f"{document} has legacy instruction: {forbidden}"


def test_nas_sources_do_not_embed_secret_values_or_require_tunnel_credentials() -> None:
    compose = nas_compose()
    for service in compose["services"].values():
        environment = service["environment"]
        assert environment["MAIMEMO_DATABASE_URL"] == (
            "${MAIMEMO_DATABASE_URL:?Set the external PostgreSQL URL}"
        )
        assert "MAIMEMO_TOKEN" not in environment
        assert "CONTROL_PLANE_API_KEY" not in environment
        assert "labels" not in service
    sources = [NAS / "compose.yaml", NAS / ".env.example", ROOT / "DEPLOYMENT.md",
               ROOT / "docs/operations.md", ROOT / "docs/tunnel-setup.md", ROOT / "README.md"]
    for source in sources:
        text = source.read_text(encoding="utf-8")
        assert not re.search(r"\b(?:ghp_|github_pat_|sk-proj-)[A-Za-z0-9_]{16,}", text)
        assert not re.search(r"(?m)^\s*(?:MAIMEMO_TOKEN|CONTROL_PLANE_API_KEY)\s*=\s*\S+", text)


def test_tunnel_guide_keeps_future_connection_behind_private_nas_security_gates() -> None:
    guide = (ROOT / "docs/tunnel-setup.md").read_text(encoding="utf-8")
    for requirement in (
        "后续", "当前私有部署", "不需要", "UGOS Pro", ">=28.0.0", "厂商明确回补",
        "NAS_IP:8000", "禁止连接 Tunnel", "http://127.0.0.1:8000/mcp",
        "本地", "未验证",
    ):
        assert requirement in guide, f"Missing future Tunnel gate: {requirement}"


@pytest.mark.parametrize("uid,gid", [(1000, 10), (10001, 10001), (1000, 10001)])
def test_secret_audit_enforces_nas_uid_and_gid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uid: int, gid: int,
) -> None:
    spec = importlib.util.spec_from_file_location("secret_audit", ROOT / "scripts"
                                                 / "check_compose_secrets.py")
    assert spec is not None and spec.loader is not None
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    token, key = tmp_path / "token", tmp_path / "key"
    token.write_text("synthetic-token", encoding="utf-8")
    key.write_text("synthetic-key", encoding="utf-8")

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
            "uid": uid, "gid": gid, "readable": 2, "readonly": 2,
        }))

    monkeypatch.setattr(audit.subprocess, "run", run)
    if (uid, gid) == (1000, 10):
        audit._audit(token, key, "synthetic-image")
    else:
        with pytest.raises(RuntimeError, match="unexpected result"):
            audit._audit(token, key, "synthetic-image")


def test_secret_audit_uses_selected_deployment_and_explicit_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = importlib.util.spec_from_file_location("secret_audit", ROOT / "scripts"
                                                 / "check_compose_secrets.py")
    assert spec is not None and spec.loader is not None
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    token, key = tmp_path / "token", tmp_path / "key"
    token.write_text("synthetic-token", encoding="utf-8")
    key.write_text("synthetic-key", encoding="utf-8")
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}", encoding="utf-8")
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert kwargs["cwd"] == tmp_path
        assert command[command.index("-f") + 1] == str(compose)
        if "run" in command:
            override = json.loads(Path(command[command.index("-f") + 3]).read_text())
            assert override["services"]["maimemo-mcp"]["image"] == "synthetic-image"
            assert override["secrets"] == {
                "maimemo_token": {"file": str(token)},
                "token_fingerprint_key": {"file": str(key)},
            }
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
            "uid": 1000, "gid": 10, "readable": 2, "readonly": 2,
        }))

    monkeypatch.setattr(audit.subprocess, "run", run)
    audit._audit(token, key, "synthetic-image", compose_file=compose)
    assert len(commands) == 2
