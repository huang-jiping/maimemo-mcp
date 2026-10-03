"""Runtime image contract for package isolation and hardened execution."""

from __future__ import annotations

import json
import subprocess

import pytest

VERSION = "0.2.0"
IMAGES = {
    "server": "maimemo-server:test",
    "mcp": "maimemo-mcp:test",
    "worker": "maimemo-worker:test",
}


def inspect_image(image: str) -> dict[str, object]:
    completed = subprocess.run(
        ["docker", "image", "inspect", image],
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)[0]


def python_probe(image: str, source: str) -> dict[str, object]:
    completed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--read-only",
            "--tmpfs",
            "/tmp:size=16m,mode=1777",
            "--entrypoint",
            "/opt/venv/bin/python",
            image,
            "-c",
            source,
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


@pytest.mark.parametrize("target,image", IMAGES.items())
def test_image_identity_and_read_only_runtime(target: str, image: str) -> None:
    inspected = inspect_image(image)
    config = inspected["Config"]

    assert config["User"] == "10001:10001"
    assert config["Labels"]["org.opencontainers.image.version"] == VERSION
    result = python_probe(
        image,
        "import json,os,tempfile; "
        "p=tempfile.NamedTemporaryFile(dir='/tmp'); "
        f"print(json.dumps({{'uid':os.getuid(),'target':{target!r},'tmp':bool(p.name)}}))",
    )
    assert result == {"uid": 10001, "target": target, "tmp": True}


@pytest.mark.parametrize(
    ("target", "present", "absent"),
    [
        ("server", "maimemo_server", ["maimemo_mcp", "maimemo_worker"]),
        ("mcp", "maimemo_mcp", ["maimemo_server", "maimemo_worker"]),
        ("worker", "maimemo_worker", ["maimemo_mcp", "maimemo_server"]),
    ],
)
def test_image_contains_only_its_runtime_package(
    target: str, present: str, absent: list[str]
) -> None:
    image = IMAGES[target]
    result = python_probe(
        image,
        "import importlib.util,json,pathlib; "
        f"names={json.dumps([present, 'maimemo', *absent])}; "
        "found={name:importlib.util.find_spec(name) is not None for name in names}; "
        "print(json.dumps({'found':found,'alembic':pathlib.Path('/app/alembic.ini').is_file(),"
        "'migrations':pathlib.Path('/app/migrations').is_dir()}))",
    )

    assert result["found"][present] is True
    assert result["found"]["maimemo"] is True
    assert all(result["found"][name] is False for name in absent)
    assert result["alembic"] is (target == "server")
    assert result["migrations"] is (target == "server")
