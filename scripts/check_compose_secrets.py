"""Audit Compose secret readability and read-only enforcement as UID 1000 / GID 10."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "compose.yaml"
_PROBE = r"""
import json
import os
from pathlib import Path

paths = [Path('/run/secrets/maimemo_token'), Path('/run/secrets/token_fingerprint_key')]
for path in paths:
    if not path.read_bytes():
        raise RuntimeError('secret file is empty')
    try:
        with path.open('ab') as stream:
            stream.write(b'x')
    except OSError:
        pass
    else:
        raise RuntimeError('secret mount is writable')
print(json.dumps({'uid': os.getuid(), 'gid': os.getgid(), 'readable': 2, 'readonly': 2}))
"""


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    source = result.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--synthetic",
        action="store_true",
        help="Use temporary non-secret values for a deployment-engine self-test",
    )
    source.add_argument("--token-file", type=Path)
    result.add_argument("--fingerprint-key-file", type=Path)
    result.add_argument("--image", default="maimemo-mcp:test")
    result.add_argument("--compose-file", type=Path, default=COMPOSE_FILE)
    return result


def _resolve_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"{label} must be a regular file")
    return resolved


def _audit(
    token_file: Path, key_file: Path, image: str, *, compose_file: Path = COMPOSE_FILE,
) -> None:
    token = _resolve_file(token_file, "Token secret")
    key = _resolve_file(key_file, "Fingerprint key secret")
    if token == key:
        raise ValueError("Token and fingerprint key must use different files")
    compose = _resolve_file(compose_file, "Compose file")
    with tempfile.TemporaryDirectory(prefix="maimemo-secret-override-") as directory:
        override = Path(directory) / "compose.override.json"
        override.write_text(json.dumps({
            "services": {"maimemo-mcp": {"image": image}},
            "secrets": {
                "maimemo_token": {"file": str(token)},
                "token_fingerprint_key": {"file": str(key)},
            },
        }), encoding="utf-8")
        _audit_compose(compose, override)


def _audit_compose(compose: Path, override: Path) -> None:
    project = f"maimemo-secret-audit-{uuid4().hex[:12]}"
    environment = dict(os.environ)
    environment.update(
        {
            "MAIMEMO_DATABASE_URL": "postgresql+psycopg://audit:unused@db.invalid/audit",
        }
    )
    command = [
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        str(compose),
        "-f",
        str(override),
        "run",
        "--rm",
        "--no-deps",
        "--entrypoint",
        "/opt/venv/bin/python",
        "maimemo-mcp",
        "-c",
        _PROBE,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=compose.parent,
            env=environment,
            text=True,
            capture_output=True,
            shell=False,
            check=True,
        )
        result = json.loads(completed.stdout.strip())
        if result != {"uid": 1000, "gid": 10, "readable": 2, "readonly": 2}:
            raise RuntimeError("Compose secret audit returned an unexpected result")
    finally:
        subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                project,
                "-f",
                str(compose),
                "-f",
                str(override),
                "down",
                "--remove-orphans",
            ],
            cwd=compose.parent,
            env=environment,
            text=True,
            capture_output=True,
            shell=False,
            check=False,
        )
    print("Compose secret audit passed: UID 1000 / GID 10 read both mounts and could write neither")


def main() -> int:
    args = parser().parse_args()
    if args.synthetic:
        if args.fingerprint_key_file is not None:
            raise ValueError("--fingerprint-key-file cannot be combined with --synthetic")
        with tempfile.TemporaryDirectory(prefix="maimemo-secret-audit-") as directory:
            root = Path(directory)
            token, key = root / "token", root / "key"
            token.write_text("synthetic-token-for-mount-audit", encoding="utf-8")
            key.write_text("synthetic-key-for-mount-audit", encoding="utf-8")
            _audit(token, key, args.image, compose_file=args.compose_file)
        return 0
    if args.fingerprint_key_file is None:
        raise ValueError("--fingerprint-key-file is required with --token-file")
    _audit(args.token_file, args.fingerprint_key_file, args.image, compose_file=args.compose_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
