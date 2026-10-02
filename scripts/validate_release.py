"""Validate a master release and promote existing registry content by digest.

Registry authentication and process output are never included in error messages.
No registry access is required to exercise the release policy functions.
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from http.client import HTTPException
from pathlib import Path
from typing import NoReturn, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

IMAGE = "ghcr.io/huang-jiping/maimemo-mcp"
REPOSITORY = "huang-jiping/maimemo-mcp"
ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))


class ReleaseError(RuntimeError):
    """Only fixed, safe messages may be exposed to the release log."""


def validate_tag(tag: str) -> str:
    if not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag):
        raise ReleaseError("release tag is invalid")
    return tag


def validate_commit(commit: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReleaseError("release commit is invalid")
    return commit


def validate_digest(digest: str) -> str:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ReleaseError("image digest is invalid")
    return digest


def require_master_ancestry(commit: str, *, cwd: Path | None = None) -> None:
    validate_commit(commit)
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, "refs/remotes/origin/master"],
            cwd=cwd, capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise ReleaseError("master ancestry check failed") from None
    if result.returncode == 1:
        raise ReleaseError("release commit is not on master")
    if result.returncode != 0:
        raise ReleaseError("master ancestry check failed")


def validate_immutable(existing: str | None, candidate: str) -> bool:
    """Return whether an absent alias needs creating; reject a conflicting alias."""
    validate_digest(candidate)
    if existing is None:
        return True
    validate_digest(existing)
    if existing != candidate:
        raise ReleaseError("immutable image tag conflicts")
    return False


class Registry(Protocol):
    def digest(self, reference: str) -> str | None: ...

    def tag(self, reference: str, digest: str) -> None: ...

    def version(self, digest: str) -> str: ...


def publish_release(registry: Registry, tag: str, commit: str, digest: str) -> str:
    validate_tag(tag)
    validate_commit(commit)
    validate_digest(digest)
    if registry.digest(digest) != digest:
        raise ReleaseError("image digest verification failed")
    aliases = (tag, f"sha-{commit}")
    # Check BOTH immutable names before writing either of them.
    missing = [validate_immutable(registry.digest(alias), digest) for alias in aliases]
    for alias, create in zip(aliases, missing, strict=True):
        if create:
            registry.tag(alias, digest)
    if any(registry.digest(alias) != digest for alias in aliases):
        raise ReleaseError("image digest verification failed")
    stable_digest = registry.digest("stable")
    if stable_digest is not None:
        validate_digest(stable_digest)
        stable_version = registry.version(stable_digest)
        try:
            if not isinstance(stable_version, str):
                raise ReleaseError("stable version metadata is invalid")
            validate_tag(stable_version)
        except ReleaseError:
            raise ReleaseError("stable version metadata is invalid") from None
        if registry.digest(stable_version) != stable_digest:
            raise ReleaseError("stable image verification failed")
        # Canonical digit strings compare numerically by length then lexical value.
        # This also avoids Python's maximum int-string length for large versions.
        stable_order = tuple((len(part), part) for part in stable_version[1:].split("."))
        release_order = tuple((len(part), part) for part in tag[1:].split("."))
        if stable_order > release_order:
            return stable_digest
        if stable_order == release_order:
            if stable_digest != digest:
                raise ReleaseError("stable image tag conflicts")
            return stable_digest
    # One tag update from the verified manifest; there is no separate stable build.
    registry.tag("stable", digest)
    if registry.digest("stable") != digest:
        raise ReleaseError("image digest verification failed")
    return digest


class GhcrRegistry:
    """Read aliases using Registry V2; copy existing manifests with Docker Buildx."""

    def __init__(self, actor: str, credential: str) -> None:
        if not actor or not credential:
            raise ReleaseError("registry authentication failed")
        basic = base64.b64encode(f"{actor}:{credential}".encode()).decode("ascii")
        request = Request(
            f"https://ghcr.io/token?service=ghcr.io&scope=repository:{REPOSITORY}:pull",
            headers={"Authorization": f"Basic {basic}"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read(65537)
            if len(body) > 65536:
                raise ValueError
            token = json.loads(body)["token"]
            if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
                raise ValueError
        except (OSError, URLError, HTTPException, ValueError, KeyError, TypeError):
            raise ReleaseError("registry authentication failed") from None
        self._authorization = f"Bearer {token}"

    def digest(self, reference: str) -> str | None:
        if reference.startswith("sha256:"):
            validate_digest(reference)
        elif reference.startswith("sha-"):
            validate_commit(reference[4:])
        elif reference != "stable":
            validate_tag(reference)
        request = Request(
            f"https://ghcr.io/v2/{REPOSITORY}/manifests/{reference}", method="HEAD",
            headers={"Accept": ACCEPT, "Authorization": self._authorization},
        )
        try:
            with urlopen(request, timeout=30) as response:
                digest = response.headers.get("Docker-Content-Digest")
        except HTTPError as error:
            error.close()
            if error.code == 404:
                return None
            raise ReleaseError("registry lookup failed") from None
        except (OSError, URLError, HTTPException, ValueError):
            raise ReleaseError("registry lookup failed") from None
        if digest is None:
            raise ReleaseError("registry lookup failed")
        return validate_digest(digest)

    def tag(self, reference: str, digest: str) -> None:
        validate_digest(digest)
        if reference.startswith("sha-"):
            validate_commit(reference[4:])
        elif reference != "stable":
            validate_tag(reference)
        try:
            result = subprocess.run(
                ["docker", "buildx", "imagetools", "create", "--prefer-index=false",
                 "--tag", f"{IMAGE}:{reference}", f"{IMAGE}@{digest}"],
                timeout=120, capture_output=True, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise ReleaseError("registry tag update failed") from None
        if result.returncode != 0:
            raise ReleaseError("registry tag update failed")

    def version(self, digest: str) -> str:
        """Read version metadata from the already resolved immutable digest."""
        validate_digest(digest)
        try:
            result = subprocess.run(
                ["docker", "buildx", "imagetools", "inspect", "--format",
                 "{{json .Image}}", f"{IMAGE}@{digest}"],
                capture_output=True, timeout=120, check=False,
            )
            if result.returncode != 0:
                raise ReleaseError("registry version lookup failed")
            image = json.loads(result.stdout)
            if not isinstance(image, dict):
                raise ValueError
            if "linux/amd64" in image:
                image = image["linux/amd64"]
            version = image["config"]["Labels"]["org.opencontainers.image.version"]
            if not isinstance(version, str):
                raise ValueError
            return validate_tag(version)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError,
                ReleaseError):
            raise ReleaseError("registry version lookup failed") from None


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ReleaseError("release arguments are invalid")


def main(argv: list[str] | None = None) -> int:
    parser = SafeParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "publish"))
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--digest")
    try:
        args = parser.parse_args(argv)
        validate_tag(args.tag)
        validate_commit(args.commit)
        require_master_ancestry(args.commit)
        if args.mode == "publish":
            if args.digest is None:
                raise ReleaseError("image digest is invalid")
            validate_digest(args.digest)
            registry = GhcrRegistry(os.environ.get("GITHUB_ACTOR", ""),
                                    os.environ.get("GITHUB_TOKEN", ""))
            digest = publish_release(registry, args.tag, args.commit, args.digest)
            if digest != args.digest:
                print("immutable release verified; newer stable preserved")
            else:
                print(f"released {args.tag} sha-{args.commit} stable {digest}")
    except ReleaseError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
