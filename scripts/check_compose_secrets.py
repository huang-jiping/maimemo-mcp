"""Audit Compose secret isolation and read-only mounts as UID 10001."""

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
SECRET_PATHS = ("/run/secrets/maimemo_token", "/run/secrets/token_fingerprint_key")
_UPSTREAM_PROBE = f"""
import json
import os
from pathlib import Path

paths = [Path(value) for value in {SECRET_PATHS!r}]
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
print(json.dumps({{'uid': os.getuid(), 'gid': os.getgid(), 'readable': 2, 'readonly': 2}}))
"""
_ISOLATION_PROBE = f"""
import json
import os
from pathlib import Path

paths = [Path(value) for value in {SECRET_PATHS!r}]
print(json.dumps({{'uid': os.getuid(), 'gid': os.getgid(),
                  'visible': sum(path.exists() for path in paths)}}))
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
    result.add_argument("--server-image", default="maimemo-server:test")
    result.add_argument("--mcp-image", default="maimemo-mcp:test")
    result.add_argument("--worker-image", default="maimemo-worker:test")
    result.add_argument("--compose-file", type=Path, default=COMPOSE_FILE)
    return result


def _resolve_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"{label} must be a regular file")
    return resolved


def _probe(
    compose: Path,
    override: Path,
    service: str,
    source: str,
    environment: dict[str, str],
) -> dict[str, int]:
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(compose),
            "-f",
            str(override),
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "/opt/venv/bin/python",
            service,
            "-c",
            source,
        ],
        cwd=compose.parent,
        env=environment,
        text=True,
        capture_output=True,
        shell=False,
        check=True,
    )
    return json.loads(completed.stdout.strip())


def _audit(
    token_file: Path,
    key_file: Path,
    *,
    server_image: str,
    mcp_image: str,
    worker_image: str,
    compose_file: Path = COMPOSE_FILE,
) -> None:
    token = _resolve_file(token_file, "Token secret")
    key = _resolve_file(key_file, "Fingerprint key secret")
    if token == key:
        raise ValueError("Token and fingerprint key must use different files")
    compose = _resolve_file(compose_file, "Compose file")
    project = f"maimemo-secret-audit-{uuid4().hex[:12]}"
    environment = dict(os.environ)
    environment.update(
        {
            "COMPOSE_PROJECT_NAME": project,
            "MAIMEMO_DATABASE_URL": "postgresql+psycopg://audit:unused@db.invalid/audit",
        }
    )
    with tempfile.TemporaryDirectory(prefix="maimemo-secret-override-") as directory:
        override = Path(directory) / "compose.override.json"
        override.write_text(
            json.dumps(
                {
                    "services": {
                        "server": {"image": server_image},
                        "migrate": {"image": server_image},
                        "mcp": {"image": mcp_image},
                        "worker": {"image": worker_image},
                    },
                    "secrets": {
                        "maimemo_token": {"file": str(token)},
                        "token_fingerprint_key": {"file": str(key)},
                    },
                }
            ),
            encoding="utf-8",
        )
        try:
            for service in ("mcp", "worker"):
                result = _probe(compose, override, service, _UPSTREAM_PROBE, environment)
                if result != {
                    "uid": 10001,
                    "gid": 10001,
                    "readable": 2,
                    "readonly": 2,
                }:
                    raise RuntimeError("Upstream secret audit returned an unexpected result")
            for service in ("server", "migrate"):
                result = _probe(compose, override, service, _ISOLATION_PROBE, environment)
                if result != {"uid": 10001, "gid": 10001, "visible": 0}:
                    raise RuntimeError(
                        "Server secret isolation audit returned an unexpected result"
                    )
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
    print(json.dumps({"uid": 10001, "upstream_services": 2, "isolated_services": 2}))


def main() -> int:
    args = parser().parse_args()
    images = {
        "server_image": args.server_image,
        "mcp_image": args.mcp_image,
        "worker_image": args.worker_image,
        "compose_file": args.compose_file,
    }
    if args.synthetic:
        if args.fingerprint_key_file is not None:
            raise ValueError("--fingerprint-key-file cannot be combined with --synthetic")
        with tempfile.TemporaryDirectory(prefix="maimemo-secret-audit-") as directory:
            root = Path(directory)
            token, key = root / "token", root / "key"
            token.write_text("synthetic-token-for-mount-audit", encoding="utf-8")
            key.write_text("synthetic-key-for-mount-audit", encoding="utf-8")
            _audit(token, key, **images)
        return 0
    if args.fingerprint_key_file is None:
        raise ValueError("--fingerprint-key-file is required with --token-file")
    _audit(args.token_file, args.fingerprint_key_file, **images)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
