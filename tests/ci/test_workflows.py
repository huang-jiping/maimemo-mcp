"""Security and release contracts for GitHub Actions workflows."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
PINNED_ACTION = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def load_workflow(name: str) -> dict[str, Any]:
    document = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    value = workflow.get("on", workflow.get(True))
    assert isinstance(value, dict)
    return value


def all_steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    return [step for job in jobs.values() for step in job.get("steps", [])]


def assert_actions_are_immutable(workflow: dict[str, Any]) -> None:
    actions = [step["uses"] for step in all_steps(workflow) if "uses" in step]
    assert actions
    assert all(PINNED_ACTION.fullmatch(action) for action in actions)


def test_ci_runs_complete_validation_without_package_write_permission() -> None:
    workflow = load_workflow("ci.yml")
    event = triggers(workflow)
    commands = "\n".join(str(step.get("run", "")) for step in all_steps(workflow))

    assert set(event) == {"pull_request", "push", "workflow_call"}
    assert event["push"]["branches"] == ["master"]
    assert workflow["permissions"] == {"contents": "read"}
    assert "packages" not in workflow["permissions"]
    job = workflow["jobs"]["test"]
    assert job["services"]["postgres"]["image"] == "postgres:15-alpine"
    assert "MAIMEMO_TEST_POSTGRES_CONTAINER" not in job["env"]
    export_container = next(
        step for step in job["steps"] if step["name"] == "Export Postgres container ID"
    )
    assert export_container["run"] == (
        'echo "MAIMEMO_TEST_POSTGRES_CONTAINER=${{ job.services.postgres.id }}" '
        '>> "$GITHUB_ENV"'
    )
    assert "uv lock --check" in commands
    assert "ruff check" in commands
    assert "mypy" in commands
    assert "pytest" in commands
    assert all(
        f"uv build --package {package}" in commands
        for package in ("maimemo", "maimemo-server", "maimemo-mcp", "maimemo-worker")
    )
    assert all(f"--target {target}" in commands for target in ("server", "mcp", "worker"))
    assert_actions_are_immutable(workflow)


def test_publish_is_gated_and_builds_exactly_three_images() -> None:
    workflow = load_workflow("publish-images.yml")
    event = triggers(workflow)
    publish = workflow["jobs"]["publish"]
    matrix = publish["strategy"]["matrix"]["include"]

    assert set(event) == {"push"}
    assert event["push"] == {"tags": ["v*"]}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["quality"] == {"uses": "./.github/workflows/ci.yml"}
    assert publish["needs"] == "quality"
    assert publish["permissions"] == {"contents": "read", "packages": "write"}
    assert matrix == [
        {"target": "server", "image": "ghcr.io/huang-jiping/maimemo-server"},
        {"target": "mcp", "image": "ghcr.io/huang-jiping/maimemo-mcp"},
        {"target": "worker", "image": "ghcr.io/huang-jiping/maimemo-worker"},
    ]
    commands = "\n".join(str(step.get("run", "")) for step in publish["steps"])
    rendered = "\n".join(str(step) for step in publish["steps"])
    assert "validate_release.py publish" in commands
    assert "validate_release.py preflight" in commands
    assert "stable" not in rendered
    build_step = next(
        step for step in publish["steps"] if step["name"] == "Build and upload digest"
    )
    assert build_step["with"]["push"] is True
    assert build_step["with"]["target"] == "${{ matrix.target }}"
    assert "push-by-digest=true" in build_step["with"]["outputs"]
    assert_actions_are_immutable(workflow)
