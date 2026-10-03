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

ALLOWED_IMAGES = frozenset(
    {
        "ghcr.io/huang-jiping/maimemo-server",
        "ghcr.io/huang-jiping/maimemo-mcp",
        "ghcr.io/huang-jiping/maimemo-worker",
    }
)
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


def validate_image(image: str) -> str:
    if image not in ALLOWED_IMAGES:
        raise ReleaseError("release image is invalid")
    return image


def validate_version_alias(alias: str) -> str:
    validate_tag(f"v{alias}")
    return alias


def require_project_version(
    tag: str,
    project: Path = Path("packages/maimemo/pyproject.toml"),
) -> None:
    validate_tag(tag)
    try:
        version = next(
            line.split('"', 2)[1]
            for line in project.read_text(encoding="utf-8").splitlines()
            if line.startswith("version = ")
        )
    except (OSError, IndexError, StopIteration):
        raise ReleaseError("project version lookup failed") from None
    if tag != f"v{version}":
        raise ReleaseError("release tag does not match project version")


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
    aliases = (tag.removeprefix("v"), f"sha-{commit}")
    # Check BOTH immutable names before writing either of them.
    missing = [validate_immutable(registry.digest(alias), digest) for alias in aliases]
    for alias, create in zip(aliases, missing, strict=True):
        if create:
            registry.tag(alias, digest)
    if any(registry.digest(alias) != digest for alias in aliases):
        raise ReleaseError("image digest verification failed")
    return digest


def promote_stable(registry: Registry, tag: str, digest: str) -> str:
    """Promote one already verified immutable release after NAS acceptance."""
    validate_tag(tag)
    validate_digest(digest)
    version_alias = tag.removeprefix("v")
    if registry.digest(version_alias) != digest:
        raise ReleaseError("immutable image tag conflicts")
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
        if registry.digest(stable_version.removeprefix("v")) != stable_digest:
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

    def __init__(self, image: str, actor: str, credential: str) -> None:
        self.image = validate_image(image)
        self.repository = image.removeprefix("ghcr.io/")
        if not actor or not credential:
            raise ReleaseError("registry authentication failed")
        basic = base64.b64encode(f"{actor}:{credential}".encode()).decode("ascii")
        request = Request(
            f"https://ghcr.io/token?service=ghcr.io&scope=repository:{self.repository}:pull",
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
            validate_version_alias(reference)
        request = Request(
            f"https://ghcr.io/v2/{self.repository}/manifests/{reference}", method="HEAD",
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
            validate_version_alias(reference)
        try:
            result = subprocess.run(
                ["docker", "buildx", "imagetools", "create", "--prefer-index=false",
                 "--tag", f"{self.image}:{reference}", f"{self.image}@{digest}"],
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
                 "{{json .Image}}", f"{self.image}@{digest}"],
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
    parser.add_argument("mode", choices=("preflight", "publish", "promote-stable"))
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--digest")
    parser.add_argument("--image")
    try:
        args = parser.parse_args(argv)
        validate_tag(args.tag)
        validate_commit(args.commit)
        require_master_ancestry(args.commit)
        require_project_version(args.tag)
        if args.mode in {"publish", "promote-stable"}:
            if args.digest is None:
                raise ReleaseError("image digest is invalid")
            if args.image is None:
                raise ReleaseError("release image is invalid")
            validate_digest(args.digest)
            registry = GhcrRegistry(
                args.image,
                os.environ.get("GITHUB_ACTOR", ""),
                os.environ.get("GITHUB_TOKEN", ""),
            )
            if args.mode == "publish":
                digest = publish_release(registry, args.tag, args.commit, args.digest)
                print(f"immutable release verified {digest}")
            else:
                digest = promote_stable(registry, args.tag, args.digest)
                print(f"stable promoted {digest}")
    except ReleaseError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
