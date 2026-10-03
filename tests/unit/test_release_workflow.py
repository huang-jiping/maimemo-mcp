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


@pytest.mark.parametrize("commit", ["a" * 39, "A" * 40, "secret", "c" * 41])
def test_noncanonical_commits_fail_with_fixed_message(release: Any, commit: str) -> None:
    with pytest.raises(release.ReleaseError, match="^release commit is invalid$"):
        release.validate_commit(commit)


@pytest.mark.parametrize(
    "digest",
    ["secret", "sha256:" + "a" * 63, "sha256:" + "A" * 64, "sha256:" + "a" * 64 + "\n"],
)
def test_malformed_digests_fail_before_registry_access(release: Any, digest: str) -> None:
    registry = Registry()
    with pytest.raises(release.ReleaseError, match="^image digest is invalid$"):
        release.publish_release(registry, "v0.2.0", COMMIT, digest)
    assert registry.writes == []


@pytest.mark.parametrize(
    ("candidate", "stable", "wanted"),
    [
        ("v0.2.0", "v0.10.0", OTHER_DIGEST),
        ("v0.10.0", "v0.2.0", DIGEST),
        ("v1.0.0", "v0.99.99", DIGEST),
        ("v1.0.1", "v1.0.10", OTHER_DIGEST),
    ],
)
def test_stable_order_compares_numeric_semver_components(
    release: Any,
    candidate: str,
    stable: str,
    wanted: str,
) -> None:
    candidate_alias = candidate.removeprefix("v")
    stable_alias = stable.removeprefix("v")
    registry = Registry(
        {candidate_alias: DIGEST, stable_alias: OTHER_DIGEST, "stable": OTHER_DIGEST},
        {DIGEST: candidate, OTHER_DIGEST: stable},
    )
    assert release.promote_stable(registry, candidate, DIGEST) == wanted
    assert registry.refs["stable"] == wanted


@pytest.mark.parametrize("version", [None, "private https://registry.invalid", "v01.2.3"])
def test_unknown_stable_version_fails_safely_without_promotion(
    release: Any,
    version: Any,
) -> None:
    registry = Registry(
        {"0.2.0": DIGEST, "stable": OTHER_DIGEST},
        {OTHER_DIGEST: version},
    )
    with pytest.raises(release.ReleaseError, match="^stable version metadata is invalid$"):
        release.promote_stable(registry, "v0.2.0", DIGEST)
    assert registry.refs["stable"] == OTHER_DIGEST
    assert registry.writes == []


def test_cli_parser_failure_does_not_echo_unknown_argument(
    release: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert release.main(
        ["preflight", "--tag", "v0.2.0", "--commit", COMMIT, "--private-auth=secret"]
    ) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "release arguments are invalid\n"
