"""replay-files against a real PostgreSQL and data root: byte-identical deliveries, originals untouched, refusals
before any write, idempotent repeats and a real SIGKILL mid-replay. Fixtures in conftest.py."""

import hashlib
import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from pathlib import Path

import psycopg
import pytest

from ecomgen import config, feeds, load, replay
from ecomgen.cli import main

ADMIN_URL = os.environ.get("ECOMGEN_TEST_DATABASE_URL", "")
EXAMPLE = Path(__file__).parents[2] / "config" / "generator.example.yaml"
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="ECOMGEN_TEST_DATABASE_URL not set")

Migrated = Callable[..., psycopg.Connection]
PARAMS = replay.Params(date(2026, 1, 5), date(2026, 1, 8), ("clickstream", "payment_settlement"), 24, 6, 20)

# replay in a child process that SIGKILLs itself just before placing delivery number N (argv[3])
CRASH_IN_REPLAY = """
import os, signal, sys, psycopg
from datetime import date
from ecomgen import config, feeds, replay
real, placed = feeds.place_file, [0]
def crash(*args, **kwargs):
    placed[0] += 1
    if placed[0] == int(sys.argv[3]):
        os.kill(os.getpid(), signal.SIGKILL)
    return real(*args, **kwargs)
feeds.place_file = crash
params = replay.Params(date(2026, 1, 5), date(2026, 1, 8), ("clickstream", "payment_settlement"), 24, 6, 20)
with psycopg.connect(sys.argv[2], autocommit=True) as conn:
    replay.replay(conn, config.load(sys.argv[1]), params)
"""


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    path = tmp_path / "generator.yaml"
    path.write_text(EXAMPLE.read_text().replace("chunk_rows: 5000", "chunk_rows: 300"))
    return path


@pytest.fixture
def small_cfg(cfg_path: Path) -> config.Config:
    return config.load(cfg_path)


@pytest.fixture
def run(migrated: Migrated, new_db: Callable[[], str], small_cfg: config.Config) -> tuple[psycopg.Connection, str]:
    url = new_db()
    c = migrated(small_cfg, url)
    load.run(c, small_cfg, "smoke")
    return c, url


def tree(root: Path, prefix: str = "") -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*"))
            if p.is_file() and str(p.relative_to(root)).startswith(prefix)
            and "staging" not in p.relative_to(root).parts}


def originals(root: Path) -> dict[str, str]:
    return {p: h for p, h in tree(root).items() if not p.startswith("deliveries/")}


def test_replay_delivers_byte_identical_copies(run: tuple[psycopg.Connection, str], small_cfg: config.Config,
                                              data_root: Path) -> None:
    conn, _ = run
    before = originals(data_root)
    r = replay.replay(conn, small_cfg, PARAMS)
    assert not r.verified_only and r.newly_delivered == r.deliveries + 1 and r.duplicates > 0
    assert originals(data_root) == before  # originals untouched
    doc = json.loads((data_root / replay.delivery_manifest_path(r.replay_id)).read_text())
    assert doc["kind"] == "replayed_delivery" and doc["run_id"] == r.run_id and len(doc["deliveries"]) == r.deliveries
    delivered = tree(data_root, f"deliveries/replay_id={r.replay_id}/")
    for d in doc["deliveries"]:
        assert delivered[d["delivered_path"]] == before[d["original_path"]] == d["sha256"]
        assert date(2026, 1, 5) <= date.fromisoformat(d["partition_date"]) <= date(2026, 1, 8)
        assert d["delivered_path"].split("/")[2] == f"arrived={d['arrived_at'][:10]}"
    in_range = {p for p in before if any(f"date=2026-01-0{k}/" in p for k in (5, 6, 7, 8))}
    assert {d["original_path"] for d in doc["deliveries"]} == in_range
    assert sum(d["duplicate"] for d in doc["deliveries"]) == r.duplicates
    assert conn.execute("SELECT state FROM generator_meta.replays").fetchone() == ("COMPLETED",)
    assert not any((data_root / "staging").iterdir())


def test_same_command_verifies_and_new_params_make_a_new_replay(run: tuple[psycopg.Connection, str],
                                                                small_cfg: config.Config, data_root: Path) -> None:
    conn, _ = run
    first = replay.replay(conn, small_cfg, PARAMS)
    snapshot = tree(data_root)
    again = replay.replay(conn, small_cfg, PARAMS)
    assert again.verified_only and again.replay_id == first.replay_id and tree(data_root) == snapshot
    other = replay.replay(conn, small_cfg, replace(PARAMS, feeds=("payment_settlement",), duplicate_pct=0))
    assert other.replay_id != first.replay_id and other.duplicates == 0
    assert {p: h for p, h in tree(data_root).items() if p in snapshot} == snapshot


@pytest.mark.parametrize("damage", ["missing", "corrupt", "manifest"])
def test_bad_originals_are_refused_before_any_write(run: tuple[psycopg.Connection, str], small_cfg: config.Config,
                                                    data_root: Path, damage: str) -> None:
    conn, _ = run
    run_id = conn.execute("SELECT run_id FROM generator_meta.generation_runs").fetchone()[0]  # type: ignore[index]
    victim = data_root / (feeds.manifest_path(run_id) if damage == "manifest" else
                          sorted(p for p in originals(data_root) if "date=2026-01-06/" in p)[0])
    if damage == "missing":
        victim.unlink()
    else:
        victim.write_bytes(victim.read_bytes() + b"x")
    with pytest.raises(replay.ReplayError, match="missing|differs"):
        replay.replay(conn, small_cfg, PARAMS)
    assert not (data_root / "deliveries").exists()
    assert conn.execute("SELECT count(*) FROM generator_meta.replays").fetchone() == (0,)


def test_sigkill_mid_replay_then_rerun_finishes_it(run: tuple[psycopg.Connection, str], cfg_path: Path,
                                                   small_cfg: config.Config, data_root: Path) -> None:
    conn, url = run
    proc = subprocess.run([sys.executable, "-c", CRASH_IN_REPLAY, str(cfg_path), url, "6"], capture_output=True,
                          timeout=120)
    assert proc.returncode == -signal.SIGKILL, proc.stderr.decode()
    assert conn.execute("SELECT state FROM generator_meta.replays").fetchone() == ("RUNNING",)
    assert conn.execute("SELECT count(*) FROM generator_meta.file_deliveries").fetchone() == (5,)
    r = replay.replay(conn, small_cfg, PARAMS)
    assert not r.verified_only and r.newly_delivered == r.deliveries + 1 - 5
    # exactly the deterministic plan, and the manifest is what an uninterrupted replay writes
    rid = r.replay_id
    run_id = r.run_id
    sha, origs = replay._originals(conn, small_cfg, run_id, data_root, PARAMS)
    plan = replay.schedule(rid, origs, PARAMS)
    expected = json.dumps(replay.delivery_manifest(rid, run_id, sha, PARAMS, plan), indent=2) + "\n"
    assert (data_root / replay.delivery_manifest_path(rid)).read_text() == expected
    delivered = tree(data_root, f"deliveries/replay_id={rid}/")
    assert set(delivered) == {d.path for d in plan} | {replay.delivery_manifest_path(rid)}
    assert all(delivered[d.path] == d.original.sha256 for d in plan)


def test_never_overwrites_deliveries(run: tuple[psycopg.Connection, str], small_cfg: config.Config,
                                     data_root: Path) -> None:
    conn, _ = run
    r = replay.replay(conn, small_cfg, PARAMS)
    victim = data_root / sorted(tree(data_root, f"deliveries/replay_id={r.replay_id}/arrived="))[0]
    victim.write_bytes(b"tampered\n")
    with pytest.raises(feeds.FeedError, match="missing or changed"):
        replay.replay(conn, small_cfg, PARAMS)
    assert victim.read_bytes() == b"tampered\n"
    # an unknown file where a new replay would deliver
    params = replace(PARAMS, delay_hours=48, duplicate_pct=0)
    rid = replay.replay_id(r.run_id, params)
    _, origs = replay._originals(conn, small_cfg, r.run_id, data_root, params)
    target = data_root / replay.schedule(rid, origs, params)[0].path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"someone else's\n")
    with pytest.raises(feeds.FeedError, match="refusing to overwrite"):
        replay.replay(conn, small_cfg, params)
    assert target.read_bytes() == b"someone else's\n"


def test_refusals(run: tuple[psycopg.Connection, str], migrated: Migrated, small_cfg: config.Config,
                  data_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn, _ = run
    with pytest.raises(replay.ReplayError, match="no clickstream files"):
        replay.replay(conn, small_cfg, replace(PARAMS, start=date(2027, 1, 1), end=date(2027, 1, 2),
                                               feeds=("clickstream",)))
    monkeypatch.setenv("ECOMGEN_DATA_ROOT", str(tmp_path))
    with pytest.raises(replay.ReplayError, match="not the current data root"):
        replay.replay(conn, small_cfg, PARAMS)
    monkeypatch.setenv("ECOMGEN_DATA_ROOT", str(data_root))
    off = replace(small_cfg, filesystem=replace(small_cfg.filesystem, csv=False, jsonl=False))
    c = migrated(off)
    load.run(c, off, "smoke")
    with pytest.raises(replay.ReplayError, match="published under None|did not publish"):
        replay.replay(c, off, PARAMS)
    conn.execute("UPDATE generator_meta.generation_runs SET state = 'FAILED', error = 'x'")
    with pytest.raises(replay.ReplayError, match="only a COMPLETED run"):
        replay.replay(conn, small_cfg, PARAMS)


def test_cli_replay(db_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("ECOMMERCE_DATABASE_URL", db_url)
    cfg = str(EXAMPLE)
    assert main(["init-db", cfg]) == 0
    assert main(["replay-files", cfg, "--from", "2026-01-05", "--to", "2026-01-06"]) == 3  # no run yet
    assert main(["generate", cfg, "--profile", "smoke"]) == 0
    capsys.readouterr()
    args = ["replay-files", cfg, "--from", "2026-01-05", "--to", "2026-01-06", "--duplicate-pct", "50"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "newly delivered" in out and "duplicates" in out
    assert main(args) == 0 and "verified (already complete)" in capsys.readouterr().out
    assert main(["replay-files", cfg, "--from", "2026-01-06", "--to", "2026-01-05"]) == 2
    assert main(["replay-files", cfg, "--from", "not-a-date", "--to", "2026-01-05"]) == 2
    assert main(["replay-files", cfg, "--from", "2027-01-01", "--to", "2027-01-02"]) == 1
