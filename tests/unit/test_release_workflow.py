"""Release policy keeps aliases immutable and stable behind NAS acceptance."""

import importlib.util
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
COMMIT = "c" * 40


@pytest.fixture
def release() -> Any:
    path = ROOT / "scripts" / "validate_release.py"
    spec = importlib.util.spec_from_file_location("release_validator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Registry:
    def __init__(
        self,
        initial: dict[str, str] | None = None,
        versions: dict[str, str] | None = None,
    ) -> None:
        self.refs = {DIGEST: DIGEST, **(initial or {})}
        self.versions = {DIGEST: "v0.2.0", OTHER_DIGEST: "v0.3.0", **(versions or {})}
        self.writes: list[tuple[str, str]] = []

    def digest(self, reference: str) -> str | None:
        return self.refs.get(reference)

    def tag(self, reference: str, digest: str) -> None:
        self.writes.append((reference, digest))
        self.refs[reference] = digest

    def version(self, digest: str) -> str:
        return self.versions[digest]


@pytest.mark.parametrize("image", sorted([
    "ghcr.io/huang-jiping/maimemo-server",
    "ghcr.io/huang-jiping/maimemo-mcp",
    "ghcr.io/huang-jiping/maimemo-worker",
]))
def test_only_three_release_images_are_allowed(release: Any, image: str) -> None:
    assert release.validate_image(image) == image
    with pytest.raises(release.ReleaseError, match="^release image is invalid$"):
        release.validate_image(f"{image}-other")


def test_project_version_must_match_release_tag(release: Any, tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\nversion = "0.2.0"\n', encoding="utf-8")
    release.require_project_version("v0.2.0", project)
    with pytest.raises(release.ReleaseError, match="does not match"):
        release.require_project_version("v0.2.1", project)


def test_publish_creates_only_immutable_version_and_commit_aliases(release: Any) -> None:
    registry = Registry()
    assert release.publish_release(registry, "v0.2.0", COMMIT, DIGEST) == DIGEST
    assert registry.refs["0.2.0"] == registry.refs[f"sha-{COMMIT}"] == DIGEST
    assert "stable" not in registry.refs


def test_publish_rejects_conflict_before_any_alias_write(release: Any) -> None:
    registry = Registry({"0.2.0": OTHER_DIGEST})
    with pytest.raises(release.ReleaseError, match="^immutable image tag conflicts$"):
        release.publish_release(registry, "v0.2.0", COMMIT, DIGEST)
    assert registry.writes == []


def test_stable_promotion_is_separate_and_never_rolls_back(release: Any) -> None:
    registry = Registry({"0.2.0": DIGEST})
    assert release.promote_stable(registry, "v0.2.0", DIGEST) == DIGEST
    assert registry.refs["stable"] == DIGEST

    newer = Registry(
        {"0.2.0": DIGEST, "0.3.0": OTHER_DIGEST, "stable": OTHER_DIGEST},
    )
    assert release.promote_stable(newer, "v0.2.0", DIGEST) == OTHER_DIGEST
    assert newer.writes == []


@pytest.mark.parametrize(
    "tag",
    ["1.2.3", "v01.2.3", "v1.2", "v1.2.3-alpha", "v1.2.3\n"],
)
def test_malformed_tags_fail_with_fixed_message(release: Any, tag: str) -> None:
    with pytest.raises(release.ReleaseError, match="^release tag is invalid$"):
        release.validate_tag(tag)
