"""Current operator documentation must describe the final multi-service project."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CURRENT_FILES = (
    ROOT / "README.md",
    ROOT / "docs" / "operations.md",
    ROOT / "docs" / "tunnel-setup.md",
    ROOT / ".env.example",
)


def current_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in CURRENT_FILES)


def test_current_docs_use_maimemo_as_project_and_final_service_names() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    text = current_text()

    assert readme.startswith("# maimemo\n")
    assert "maimemo-learning-foundation" not in text
    assert "maimemo-mcp:local" not in text
    assert "http://maimemo-mcp:8000" not in text
    assert "同一镜像" not in text
    assert "docker compose run --rm migrate upgrade head" in text


def test_current_docs_name_three_images_and_four_services() -> None:
    text = current_text()

    assert all(
        image in text
        for image in (
            "ghcr.io/huang-jiping/maimemo-server",
            "ghcr.io/huang-jiping/maimemo-mcp",
            "ghcr.io/huang-jiping/maimemo-worker",
        )
    )
    assert all(f"`{service}`" in text for service in ("migrate", "server", "mcp", "worker"))
