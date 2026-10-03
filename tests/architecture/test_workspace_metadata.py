"""Contracts for the transitional uv workspace metadata."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as source:
        return tomllib.load(source)


def workspace_members() -> set[str]:
    metadata = load_toml(ROOT / "pyproject.toml")
    patterns = metadata.get("tool", {}).get("uv", {}).get("workspace", {}).get("members", [])
    return {
        member.relative_to(ROOT).as_posix()
        for pattern in patterns
        for member in ROOT.glob(pattern)
        if (member / "pyproject.toml").is_file()
    }


def workspace_member_metadata() -> dict[str, dict[str, Any]]:
    return {
        member: load_toml(ROOT / member / "pyproject.toml")
        for member in sorted(workspace_members())
    }


def workspace_member_versions() -> set[str]:
    return {
        str(metadata["project"]["version"])
        for metadata in workspace_member_metadata().values()
    }


def test_workspace_declares_expected_members() -> None:
    assert workspace_members() == {
        "packages/maimemo",
        "packages/maimemo-server",
        "packages/maimemo-worker",
    }


def test_initial_package_versions_are_aligned() -> None:
    assert workspace_member_versions() == {"0.2.0"}


def test_workspace_members_use_hatchling_src_layout() -> None:
    expected_import_packages = {
        "packages/maimemo": "src/maimemo",
        "packages/maimemo-server": "src/maimemo_server",
        "packages/maimemo-worker": "src/maimemo_worker",
    }

    all_metadata = workspace_member_metadata()
    assert set(all_metadata) == set(expected_import_packages)

    for member, metadata in all_metadata.items():
        assert metadata["build-system"] == {
            "requires": ["hatchling"],
            "build-backend": "hatchling.build",
        }
        assert metadata["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
            expected_import_packages[member]
        ]


def test_runtime_placeholders_depend_on_core_workspace_package() -> None:
    metadata = workspace_member_metadata()

    for member in ("packages/maimemo-server", "packages/maimemo-worker"):
        assert "maimemo>=0.2.0,<0.3" in metadata[member]["project"]["dependencies"]
