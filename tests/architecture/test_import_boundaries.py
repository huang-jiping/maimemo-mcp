"""Static dependency boundaries for the multi-package workspace."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def imported_modules(source_file: Path) -> set[str]:
    tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def forbidden_imports(source_root: str, forbidden: set[str]) -> list[str]:
    violations: list[str] = []
    for source_file in sorted((ROOT / source_root).rglob("*.py")):
        for module in imported_modules(source_file):
            if any(module == name or module.startswith(f"{name}.") for name in forbidden):
                violations.append(f"{source_file.relative_to(ROOT).as_posix()}: {module}")
    return violations


def advisory_key_import(package: str, module: str) -> str | None:
    source_file = ROOT / "packages" / package / "src" / Path(*module.split("."))
    source_file = source_file.with_suffix(".py")
    for imported in imported_modules(source_file):
        if imported.endswith("ingestion.locks"):
            return imported
    return None


def test_core_never_imports_runtime_packages() -> None:
    assert forbidden_imports(
        "packages/maimemo/src",
        {"maimemo_mcp", "maimemo_server", "maimemo_worker"},
    ) == []


def test_repository_uses_core_advisory_key() -> None:
    assert advisory_key_import("maimemo", "maimemo.storage.repositories") == (
        "maimemo.ingestion.locks"
    )


def test_worker_never_imports_mcp() -> None:
    assert forbidden_imports("packages/maimemo-worker/src", {"maimemo_mcp"}) == []


def test_worker_uses_core_advisory_key() -> None:
    assert advisory_key_import("maimemo-worker", "maimemo_worker.scheduler") == (
        "maimemo.ingestion.locks"
    )


def test_mcp_never_imports_other_runtime_packages() -> None:
    assert forbidden_imports(
        "packages/maimemo-mcp/src", {"maimemo_server", "maimemo_worker"}
    ) == []


def test_mcp_contains_only_protocol_adapter_modules() -> None:
    package = ROOT / "packages" / "maimemo-mcp" / "src" / "maimemo_mcp"
    source_entries = {
        path.name
        for path in package.iterdir()
        if path.name != "__pycache__" and (path.is_file() or any(path.rglob("*.py")))
    }
    assert source_entries <= {
        "__init__.py",
        "config.py",
        "dependencies.py",
        "envelopes.py",
        "health.py",
        "py.typed",
        "runtime.py",
        "server.py",
        "tools",
    }


def test_server_never_imports_other_runtime_packages() -> None:
    assert forbidden_imports(
        "packages/maimemo-server/src", {"maimemo_mcp", "maimemo_worker"}
    ) == []
