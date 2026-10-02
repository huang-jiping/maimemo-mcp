"""Release gates protect immutable aliases and promote only a verified digest."""

import importlib.util
import io
import subprocess
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

import pytest
import yaml

ROOT = Path(__file__).parents[2]
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
COMMIT = "c" * 40
IMAGE = "ghcr.io/huang-jiping/maimemo-mcp"


def workflow(name: str) -> dict[str, Any]:
    path = ROOT / ".github" / "workflows" / name
    assert path.is_file(), f"Missing workflow: {name}"
    # GitHub's YAML treats `on` as a string, unlike PyYAML's YAML 1.1 SafeLoader.
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


@pytest.fixture
def release() -> Any:
    path = ROOT / "scripts" / "validate_release.py"
    assert path.is_file(), "Missing release validator"
    spec = importlib.util.spec_from_file_location("release_validator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ci_runs_locked_python_quality_gates_with_real_postgres() -> None:
    ci = workflow("ci.yml")
    assert set(ci["on"]) == {"pull_request", "push", "workflow_call"}
    assert ci["on"]["push"]["branches"] == ["**"]
    assert ci["permissions"] == {"contents": "read"}
    job = ci["jobs"]["quality"]
    assert "services" not in job  # Integration tests discover the Compose-managed container.
    postgres = yaml.safe_load((ROOT / "compose.test.yaml").read_text(encoding="utf-8"))[
        "services"]["postgres"]
    assert postgres["image"] == "postgres:15-alpine"
    assert postgres["ports"] == ["127.0.0.1:55432:5432"]
    assert postgres["environment"] == {
        "POSTGRES_DB": "maimemo_test", "POSTGRES_USER": "maimemo_test",
        "POSTGRES_PASSWORD": "test_only",
    }
    assert "pg_isready" in postgres["healthcheck"]["test"][-1]
    steps = job["steps"]
    python = next(step for step in steps if step.get("uses", "").startswith(
        "actions/setup-python@"))
    assert python["with"]["python-version"] == "3.12"
    commands = [step["run"] for step in steps if "run" in step]
    for command in (
        "uv sync --frozen", "uv run --frozen pytest -q", "uv run --frozen ruff check .",
        "uv run --frozen mypy src", "docker build --platform linux/amd64",
        "docker compose -f compose.test.yaml up -d --wait",
    ):
        assert any(command in run for run in commands), command
    build = next(index for index, run in enumerate(commands) if "docker build" in run)
    postgres_start = next(index for index, run in enumerate(commands) if " up -d --wait" in run)
    tests = commands.index("uv run --frozen pytest -q")
    assert postgres_start < tests and build < tests
    assert "-t maimemo-mcp:test" in commands[build]
    assert job["steps"][-1]["if"] == "always()"
    assert job["steps"][-1]["run"] == "docker compose -f compose.test.yaml down --volumes"


@pytest.mark.parametrize("name", ["ci.yml", "publish-image.yml"])
def test_actions_are_official_full_sha_pins(name: str) -> None:
    import re

    pins = {
        "actions/checkout": "11bd71901bbe5b1630ceea73d27597364c9af683",
        "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
        "astral-sh/setup-uv": "d0cc045d04ccac9d8b7881df0226f9e82c39688e",
        "docker/setup-buildx-action": "e468171a9de216ec08956ac3ada2f0791b6bd435",
        "docker/login-action": "184bdaa0721073962dff0199f1fb9940f07167d1",
        "docker/build-push-action": "263435318d21b8e681c14492fe198d362a7d2c83",
    }
    for job in workflow(name)["jobs"].values():
        for step in job.get("steps", []):
            if "uses" in step:
                repository, sha = step["uses"].split("@")
                assert re.fullmatch(r"[0-9a-f]{40}", sha)
                assert pins[repository] == sha


def test_release_repeats_ci_and_serializes_master_tag_releases() -> None:
    publish = workflow("publish-image.yml")
    assert publish["on"] == {"push": {"tags": ["v[0-9]+.[0-9]+.[0-9]+"]}}
    assert publish["permissions"] == {"contents": "read"}
    assert publish["concurrency"] == {
        "group": "ghcr-maimemo-mcp-stable", "cancel-in-progress": "false",
    }
    assert publish["jobs"]["quality"]["uses"] == "./.github/workflows/ci.yml"
    job = publish["jobs"]["publish"]
    assert job["needs"] == "quality"
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    assert publish["env"]["IMAGE"] == IMAGE
    steps = job["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith(
        "actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == "0"
    commands = "\n".join(step.get("run", "") for step in steps)
    assert "refs/heads/master:refs/remotes/origin/master" in commands
    assert "python scripts/validate_release.py preflight" in commands
    assert commands.index(" preflight") < commands.index(" publish")
    assert "secrets." not in commands
    assert {secret for step in steps for secret in step.get("env", {}).values()
            if "secrets." in secret} <= {"${{ secrets.GITHUB_TOKEN }}"}


def test_release_builds_once_by_digest_then_promotes_without_rebuilding() -> None:
    job = workflow("publish-image.yml")["jobs"]["publish"]
    builds = [step for step in job["steps"] if step.get("uses", "").startswith(
        "docker/build-push-action@")]
    assert len(builds) == 1
    build = builds[0]
    assert build["with"]["platforms"] == "linux/amd64"
    assert build["with"]["outputs"] == (
        "type=image,name=${{ env.IMAGE }},push-by-digest=true,name-canonical=true,push=true"
    )
    assert "tags" not in build["with"]
    assert build["with"]["provenance"] == "false"
    assert "SOURCE_REVISION=" in build["with"]["build-args"]
    assert "IMAGE_VERSION=" in build["with"]["build-args"]
    final = job["steps"][-1]
    assert "python scripts/validate_release.py publish" in final["run"]
    assert final["env"]["RELEASE_DIGEST"] == "${{ steps.build.outputs.digest }}"


@pytest.mark.parametrize("tag", ["v0.1.0", "v1.2.3", "v12.200.3000"])
def test_accepts_exact_release_tags(release: Any, tag: str) -> None:
    assert release.validate_tag(tag) == tag


@pytest.mark.parametrize("tag", [
    "1.2.3", "v01.2.3", "v1.02.3", "v1.2.03", "v1.2", "v1.2.3-alpha",
    "v1.2.3+build", "v1.2.3\n", "v1.2.3;echo secret", "v１.2.3",
])
def test_rejects_malformed_tag_without_echoing_input(release: Any, tag: str) -> None:
    with pytest.raises(release.ReleaseError) as error:
        release.validate_tag(tag)
    assert str(error.value) == "release tag is invalid"


@pytest.mark.parametrize("commit", ["a" * 39, "A" * 40, "secret", "c" * 41])
def test_rejects_noncanonical_commit(release: Any, commit: str) -> None:
    with pytest.raises(release.ReleaseError, match="^release commit is invalid$"):
        release.validate_commit(commit)


@pytest.mark.parametrize("existing,writes", [(None, True), (DIGEST, False)])
def test_immutable_tag_absence_or_same_digest(release: Any, existing: str | None,
                                            writes: bool) -> None:
    assert release.validate_immutable(existing, DIGEST) is writes


def test_immutable_conflict_is_fixed_safe_failure(release: Any) -> None:
    with pytest.raises(release.ReleaseError) as error:
        release.validate_immutable(OTHER_DIGEST, DIGEST)
    assert str(error.value) == "immutable image tag conflicts"


def test_master_ancestry_uses_real_git_history(release: Any, tmp_path: Path) -> None:
    def git(*args: str) -> str:
        result = subprocess.run(["git", *args], cwd=tmp_path, check=True,
                                capture_output=True, text=True)
        return result.stdout.strip()

    git("init", "-q")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "--allow-empty", "-m", "master base")
    base = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/master", base)
    release.require_master_ancestry(base, cwd=tmp_path)
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "--allow-empty", "-m", "unmerged commit")
    with pytest.raises(release.ReleaseError, match="^release commit is not on master$"):
        release.require_master_ancestry(git("rev-parse", "HEAD"), cwd=tmp_path)


class Registry:
    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.refs = {DIGEST: DIGEST, **(initial or {})}
        self.events: list[tuple[str, str]] = []

    def digest(self, reference: str) -> str | None:
        self.events.append(("read", reference))
        return self.refs.get(reference)

    def tag(self, reference: str, digest: str) -> None:
        self.events.append(("write", reference))
        self.refs[reference] = digest


def test_publish_promotes_verified_immutable_digest_last(release: Any) -> None:
    registry = Registry()
    assert release.publish_release(registry, "v0.1.0", COMMIT, DIGEST) == DIGEST
    assert registry.refs["v0.1.0"] == registry.refs[f"sha-{COMMIT}"] == (
        registry.refs["stable"]) == DIGEST
    assert registry.events == [
        ("read", DIGEST), ("read", "v0.1.0"), ("read", f"sha-{COMMIT}"),
        ("write", "v0.1.0"), ("write", f"sha-{COMMIT}"),
        ("read", "v0.1.0"), ("read", f"sha-{COMMIT}"),
        ("write", "stable"), ("read", "stable"),
    ]


def test_publish_retry_preserves_same_digest_immutable_tags(release: Any) -> None:
    registry = Registry({"v0.1.0": DIGEST, f"sha-{COMMIT}": DIGEST})
    release.publish_release(registry, "v0.1.0", COMMIT, DIGEST)
    assert [event for event in registry.events if event[0] == "write"] == [
        ("write", "stable")]


@pytest.mark.parametrize("reference", ["v0.1.0", f"sha-{COMMIT}"])
def test_conflict_blocks_all_tag_writes(release: Any, reference: str) -> None:
    registry = Registry({reference: OTHER_DIGEST})
    with pytest.raises(release.ReleaseError, match="^immutable image tag conflicts$"):
        release.publish_release(registry, "v0.1.0", COMMIT, DIGEST)
    assert all(event[0] == "read" for event in registry.events)


def test_failed_immutable_verification_never_moves_stable(release: Any) -> None:
    class LostWrite(Registry):
        def tag(self, reference: str, digest: str) -> None:
            self.events.append(("write", reference))

    registry = LostWrite()
    with pytest.raises(release.ReleaseError, match="^image digest verification failed$"):
        release.publish_release(registry, "v0.1.0", COMMIT, DIGEST)
    assert ("write", "stable") not in registry.events


def test_cli_rejects_arbitrary_input_with_safe_error(release: Any, capsys: Any) -> None:
    assert release.main(["preflight", "--tag", "secret", "--commit", COMMIT]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "release tag is invalid\n"


@pytest.mark.parametrize("digest", ["secret", "sha256:" + "a" * 63,
                                   "sha256:" + "A" * 64, "sha256:" + "a" * 64 + "\n"])
def test_rejects_malformed_digest_before_registry_access(release: Any, digest: str) -> None:
    registry = Registry()
    with pytest.raises(release.ReleaseError, match="^image digest is invalid$"):
        release.publish_release(registry, "v0.1.0", COMMIT, digest)
    assert registry.events == []


def test_cli_parser_failure_does_not_echo_unknown_argument(release: Any, capsys: Any) -> None:
    assert release.main(["preflight", "--tag", "v0.1.0", "--commit", COMMIT,
                         "--private-auth=secret"]) == 1
    assert capsys.readouterr().err == "release arguments are invalid\n"


class Response(io.BytesIO):
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        super().__init__(body)
        self.headers = headers or {}


def ghcr(release: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(release, "urlopen", lambda *args, **kwargs: Response(b'{"token":"safe"}'))
    return release.GhcrRegistry("test-actor", "synthetic-auth")


def test_registry_reads_manifest_digest_using_authenticated_head(release: Any,
                                                               monkeypatch: Any) -> None:
    registry = ghcr(release, monkeypatch)

    def request(request: Any, **kwargs: Any) -> Response:
        assert request.get_method() == "HEAD"
        assert request.full_url == (
            "https://ghcr.io/v2/huang-jiping/maimemo-mcp/manifests/v0.1.0")
        assert request.get_header("Authorization") == "Bearer safe"
        assert "application/vnd.oci.image.manifest.v1+json" in request.get_header("Accept")
        assert kwargs == {"timeout": 30}
        return Response(b"", {"Docker-Content-Digest": DIGEST})

    monkeypatch.setattr(release, "urlopen", request)
    assert registry.digest("v0.1.0") == DIGEST


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_only_registry_404_means_immutable_tag_absent(release: Any,
                                                    monkeypatch: Any, status: int) -> None:
    registry = ghcr(release, monkeypatch)

    def request(*args: Any, **kwargs: Any) -> None:
        raise HTTPError("https://registry.invalid/private-auth", status, "secret", {},
                        io.BytesIO(b"arbitrary-response-secret"))

    monkeypatch.setattr(release, "urlopen", request)
    if status == 404:
        assert registry.digest("v0.1.0") is None
    else:
        with pytest.raises(release.ReleaseError) as error:
            registry.digest("v0.1.0")
        assert str(error.value) == "registry lookup failed"


def test_registry_network_failure_has_fixed_safe_message(release: Any, monkeypatch: Any) -> None:
    registry = ghcr(release, monkeypatch)

    def request(*args: Any, **kwargs: Any) -> None:
        raise URLError("synthetic-auth")

    monkeypatch.setattr(release, "urlopen", request)
    with pytest.raises(release.ReleaseError, match="^registry lookup failed$"):
        registry.digest("v0.1.0")


@pytest.mark.parametrize("body", [b"synthetic-auth", b'{"token":null}', b"{}",
                                  b'{"token":"safe\\nsecret"}', b" " * 65537],
                         ids=["not-json", "null-token", "missing-token", "newline", "oversize"])
def test_registry_auth_errors_never_return_arbitrary_body(release: Any, monkeypatch: Any,
                                                        body: bytes) -> None:
    monkeypatch.setattr(release, "urlopen", lambda *args, **kwargs: Response(body))
    with pytest.raises(release.ReleaseError, match="^registry authentication failed$"):
        release.GhcrRegistry("actor", "synthetic-auth")


@pytest.mark.parametrize("digest", [None, "synthetic-auth"])
def test_registry_missing_or_invalid_digest_header_is_not_absence(release: Any,
                                                                monkeypatch: Any,
                                                                digest: str | None) -> None:
    registry = ghcr(release, monkeypatch)
    headers = {} if digest is None else {"Docker-Content-Digest": digest}
    monkeypatch.setattr(release, "urlopen", lambda *args, **kwargs: Response(b"", headers))
    with pytest.raises(release.ReleaseError):
        registry.digest("v0.1.0")


def test_registry_promotion_uses_exact_existing_digest(release: Any, monkeypatch: Any) -> None:
    registry = ghcr(release, monkeypatch)

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert command == ["docker", "buildx", "imagetools", "create", "--prefer-index=false",
                           "--tag", f"{IMAGE}:stable", f"{IMAGE}@{DIGEST}"]
        assert kwargs == {"capture_output": True, "timeout": 120, "check": False}
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(release.subprocess, "run", run)
    registry.tag("stable", DIGEST)


def test_registry_tag_failure_does_not_expose_process_output(release: Any, monkeypatch: Any,
                                                           capsys: Any) -> None:
    registry = ghcr(release, monkeypatch)
    monkeypatch.setattr(release.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 1, b"secret", b"synthetic-auth"))
    with pytest.raises(release.ReleaseError, match="^registry tag update failed$"):
        registry.tag("stable", DIGEST)
    assert capsys.readouterr().out == ""


def test_docker_preserves_runtime_identity_and_adds_oci_metadata() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for value in (
        "ARG SOURCE_REVISION", "ARG IMAGE_VERSION", "org.opencontainers.image.source",
        "https://github.com/huang-jiping/maimemo-mcp", "org.opencontainers.image.revision",
        "org.opencontainers.image.version", "USER 1000:10",
        'ENTRYPOINT ["/opt/venv/bin/python", "-m", "maimemo_mcp.runtime"]',
    ):
        assert value in dockerfile
