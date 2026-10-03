"""GHCR release transport failures remain fixed, safe, and digest-bound."""

import importlib.util
import io
import json
import subprocess
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

ROOT = Path(__file__).parents[2]
IMAGE = "ghcr.io/huang-jiping/maimemo-mcp"
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64


@pytest.fixture
def release() -> Any:
    path = ROOT / "scripts" / "validate_release.py"
    spec = importlib.util.spec_from_file_location("release_transport_validator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Response(io.BytesIO):
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        super().__init__(body)
        self.headers = headers or {}


def ghcr(release: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(
        release,
        "urlopen",
        lambda *args, **kwargs: Response(b'{"token":"safe"}'),
    )
    return release.GhcrRegistry(IMAGE, "test-actor", "synthetic-auth")


def test_registry_reads_version_alias_with_authenticated_head(
    release: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ghcr(release, monkeypatch)

    def request(request: Any, **kwargs: Any) -> Response:
        assert request.get_method() == "HEAD"
        assert request.full_url == (
            "https://ghcr.io/v2/huang-jiping/maimemo-mcp/manifests/0.2.0"
        )
        assert request.get_header("Authorization") == "Bearer safe"
        assert "application/vnd.oci.image.manifest.v1+json" in request.get_header("Accept")
        assert kwargs == {"timeout": 30}
        return Response(b"", {"Docker-Content-Digest": DIGEST})

    monkeypatch.setattr(release, "urlopen", request)
    assert registry.digest("0.2.0") == DIGEST


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_only_registry_404_means_alias_absent(
    release: Any,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    registry = ghcr(release, monkeypatch)

    def request(*args: Any, **kwargs: Any) -> None:
        raise HTTPError(
            "https://registry.invalid/private-auth",
            status,
            "secret",
            {},
            io.BytesIO(b"arbitrary-response-secret"),
        )

    monkeypatch.setattr(release, "urlopen", request)
    if status == 404:
        assert registry.digest("0.2.0") is None
    else:
        with pytest.raises(release.ReleaseError, match="^registry lookup failed$"):
            registry.digest("0.2.0")


def test_registry_network_failure_has_fixed_safe_message(
    release: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ghcr(release, monkeypatch)
    monkeypatch.setattr(
        release,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(URLError("synthetic-auth")),
    )
    with pytest.raises(release.ReleaseError, match="^registry lookup failed$"):
        registry.digest("0.2.0")


@pytest.mark.parametrize(
    "body",
    [
        b"synthetic-auth",
        b'{"token":null}',
        b"{}",
        b'{"token":"safe\\nsecret"}',
        b" " * 65537,
    ],
    ids=["not-json", "null-token", "missing-token", "newline", "oversize"],
)
def test_registry_auth_errors_never_expose_response_body(
    release: Any,
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
) -> None:
    monkeypatch.setattr(release, "urlopen", lambda *args, **kwargs: Response(body))
    with pytest.raises(release.ReleaseError, match="^registry authentication failed$"):
        release.GhcrRegistry(IMAGE, "actor", "synthetic-auth")


@pytest.mark.parametrize("digest", [None, "synthetic-auth"])
def test_missing_or_invalid_digest_header_is_not_absence(
    release: Any,
    monkeypatch: pytest.MonkeyPatch,
    digest: str | None,
) -> None:
    registry = ghcr(release, monkeypatch)
    headers = {} if digest is None else {"Docker-Content-Digest": digest}
    monkeypatch.setattr(release, "urlopen", lambda *args, **kwargs: Response(b"", headers))
    with pytest.raises(release.ReleaseError) as caught:
        registry.digest("0.2.0")
    assert str(caught.value) in {"registry lookup failed", "image digest is invalid"}
    assert "synthetic-auth" not in str(caught.value)


def test_registry_tag_uses_exact_existing_digest(
    release: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ghcr(release, monkeypatch)

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert command == [
            "docker",
            "buildx",
            "imagetools",
            "create",
            "--prefer-index=false",
            "--tag",
            f"{IMAGE}:stable",
            f"{IMAGE}@{DIGEST}",
        ]
        assert kwargs == {"capture_output": True, "timeout": 120, "check": False}
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(release.subprocess, "run", run)
    registry.tag("stable", DIGEST)


def test_registry_tag_failure_does_not_expose_process_output(
    release: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ghcr(release, monkeypatch)
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, b"private", b"synthetic-auth"
        ),
    )
    with pytest.raises(release.ReleaseError, match="^registry tag update failed$"):
        registry.tag("stable", DIGEST)


@pytest.mark.parametrize("wrapped", [False, True])
def test_registry_version_reads_config_at_exact_digest(
    release: Any,
    monkeypatch: pytest.MonkeyPatch,
    wrapped: bool,
) -> None:
    registry = ghcr(release, monkeypatch)
    config = {"config": {"Labels": {"org.opencontainers.image.version": "v0.2.0"}}}
    payload = {"linux/amd64": config} if wrapped else config

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert command == [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            "--format",
            "{{json .Image}}",
            f"{IMAGE}@{OTHER_DIGEST}",
        ]
        assert kwargs == {"capture_output": True, "timeout": 120, "check": False}
        return subprocess.CompletedProcess(command, 0, json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(release.subprocess, "run", run)
    assert registry.version(OTHER_DIGEST) == "v0.2.0"


@pytest.mark.parametrize(
    "payload",
    [
        b"synthetic-auth https://registry.invalid",
        b"null",
        b"{}",
        b'{"config":{"Labels":{}}}',
        b'{"config":{"Labels":{"org.opencontainers.image.version":"private"}}}',
    ],
)
def test_registry_version_errors_have_fixed_safe_message(
    release: Any,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
) -> None:
    registry = ghcr(release, monkeypatch)
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, payload),
    )
    with pytest.raises(release.ReleaseError, match="^registry version lookup failed$"):
        registry.version(OTHER_DIGEST)
