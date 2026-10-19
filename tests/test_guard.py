"""The Claude Code safety hook (.claude/hooks/guard.py): blocks the irreversible commands it lists, nothing else."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

GUARD = Path(__file__).parents[1] / ".claude" / "hooks" / "guard.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("guard", GUARD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load()


@pytest.mark.parametrize("command", [
    "git tag -d v0.2.0-homelab",
    "git tag -f v0.2.0-homelab abc123",
    "git tag --delete v0.3.0-homelab",
    "git push origin --delete v0.2.0-homelab",
    "git push origin :refs/tags/v0.2.0-homelab",
    "git push --force origin main",
    "git push -f origin v0.2.0-homelab",
    "git push origin +main",
    "gh release delete v0.2.0-homelab",
    "gh api -X DELETE repos/o/r/git/refs/tags/v0.2.0-homelab",
    "docker rm -f vlsim-tms-postgres-1",
    "docker compose -p vlsim down",
    "docker stop vlsim-redpanda-1",
    "docker exec vlsim-tms-postgres-1 psql -c 'drop table x'",
    "docker system prune -a",
    "psql postgresql://u:p@127.0.0.1:55432/tms",
    "psql -h 127.0.0.1 -p 55432 -U x",
    "rm -rf /srv/homelab/ecommerce/data",
    "find /srv/homelab/ecommerce -name '*.jsonl' -delete",
    "ssh serveradmin@homelab 'ls'",
    "cd x && ssh host",
    "dropdb ecommerce_oltp",
    "psql -c 'DROP DATABASE ecomgen_practice'",
])
def test_blocks(command: str) -> None:
    assert guard.blocked(command)


@pytest.mark.parametrize("command", [
    "git tag -a v0.3.0-homelab 8f667d4 -m 'release'",
    "git push origin v0.3.0-homelab",
    "git push -u origin feat/x",
    "git push --force-with-lease origin feat/x",
    "git tag -l",
    "docker ps --format '{{.Names}}'",
    "docker logs vlsim-tms-service-1",
    "docker rm -f ecomgen-pg18-test",
    "docker exec ecomgen-pg18-test dropdb -U ecomgen ecommerce_homelab_test",
    "grep -n 55432 scripts/homelab_bench.py",
    "ls /srv/homelab/ecommerce",
    "uv run pytest -q",
    "git commit -m 'docs: ssh notes'",
])
def test_allows(command: str) -> None:
    assert guard.blocked(command) is None


@pytest.mark.parametrize("stdin, code", [
    (json.dumps({"tool_input": {"command": "git tag -d v0.2.0-homelab"}}), 2),
    (json.dumps({"tool_input": {"command": "ls"}}), 0),
    ("not json", 0),  # fails open
    (json.dumps({"tool_input": {}}), 0),
])
# the hook runs under whatever python3 is on PATH: also the system one (3.9 on macOS)
@pytest.mark.parametrize("python", sorted({sys.executable} | {p for p in ["/usr/bin/python3"] if Path(p).exists()}))
def test_hook_protocol(stdin: str, code: int, python: str) -> None:
    r = subprocess.run([python, str(GUARD)], input=stdin, capture_output=True, text=True, timeout=10)
    assert r.returncode == code and (code == 0 or "Blocked" in r.stderr)
