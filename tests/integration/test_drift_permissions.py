"""Linux replacement inodes stay readable by the real service UID."""

import subprocess
from pathlib import Path


def test_consecutive_atomic_drift_publications_are_readable_by_uid10001() -> None:
    scripts = Path(__file__).parents[2] / "scripts"
    probe = r'''
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
sys.path.insert(0, '/probe')
from check_openapi_drift import _write_state, compare_openapi
root = Path('/tmp/drift-permission-probe')
root.mkdir(mode=0o755)
pinned = Path('/app/openapi/maimemo-api.yaml').read_bytes()
report = compare_openapi(pinned, pinned)
reader = """
import os
from pathlib import Path
from datetime import UTC, datetime
from maimemo_mcp.health import _drift_health, pinned_schema_hash
assert os.getuid() == 10001
health = _drift_health(Path('/tmp/drift-permission-probe/state.json'),
    current_schema_hash=pinned_schema_hash(), now=datetime.now(UTC))
assert health['drift_status'] == 'none', health['drift_status']
"""
def demote():
    os.setgid(10001)
    os.setuid(10001)
for _ in range(2):
    _write_state(root / 'state.json', report, datetime.now(UTC))
    subprocess.run([sys.executable, '-c', reader], preexec_fn=demote, check=True)
print('UID10001 readable after 2 atomic replacements')
'''
    result = subprocess.run([
        "docker", "run", "--rm", "--user", "0:0", "--entrypoint", "/opt/venv/bin/python",
        "--mount", f"type=bind,source={scripts.resolve()},target=/probe,readonly",
        "maimemo-mcp:test", "-c", probe,
    ], capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "UID10001 readable after 2 atomic replacements" in result.stdout
