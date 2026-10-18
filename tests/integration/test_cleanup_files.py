"""cleanup-files against a real PostgreSQL and data root: dry run, confirmation token, exactly-the-candidates
deletion, kept manifests and unknown files, audit report, symlink and root refusals, and a real SIGKILL mid-delete.
Fixtures in conftest.py."""

import hashlib
import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

import psycopg
import pytest

from ecomgen import cleanup, config, db, feeds, load, replay, validate
from ecomgen.cli import main

ADMIN_URL = os.environ.get("ECOMGEN_TEST_DATABASE_URL", "")
EXAMPLE = Path(__file__).parents[2] / "config" / "generator.example.yaml"
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="ECOMGEN_TEST_DATABASE_URL not set")

Migrated = Callable[..., psycopg.Connection]
PARAMS = replay.Params(date(2026, 1, 5), date(2026, 1, 8), ("clickstream", "payment_settlement"), 24, 6, 20)

# cleanup --delete in a child process that SIGKILLs itself after the Nth directory fsync (argv[4]), i.e. right
# after unlinking a file and before its ledger row is marked
CRASH_IN_CLEANUP = """
import os, signal, sys, psycopg
from ecomgen import cleanup, config, feeds
real, n = feeds._fsync_dir, [0]
def crash(path):
    real(path)
    n[0] += 1
    if n[0] == int(sys.argv[4]):
        os.kill(os.getpid(), signal.SIGKILL)
feeds._fsync_dir = crash
with psycopg.connect(sys.argv[2], autocommit=True) as conn:
    cleanup.run(conn, config.load(sys.argv[1]), cleanup.Target("replay", sys.argv[3]), sys.argv[5])
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
def setup(migrated: Migrated, new_db: Callable[[], str], small_cfg: config.Config) -> tuple[psycopg.Connection, str,
                                                                                           str, str]:
    """A COMPLETED run with files, plus a COMPLETED replay of part of them: (conn, url, run_id, replay_id)."""
    url = new_db()
    c = migrated(small_cfg, url)
    run_id = load.run(c, small_cfg, "smoke").run_id
    return c, url, run_id, replay.replay(c, small_cfg, PARAMS).replay_id


def tree(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*"))
            if p.is_file() and not p.is_symlink() and "staging" not in p.relative_to(root).parts}


def test_dry_run_deletes_nothing_and_token_is_stable(setup: tuple[psycopg.Connection, str, str, str],
                                                     small_cfg: config.Config, data_root: Path) -> None:
    conn, _, run_id, rid = setup
    before = tree(data_root)
    plan, result = cleanup.run(conn, small_cfg, cleanup.Target("replay", rid), None)
    assert result is None and tree(data_root) == before
    assert plan.candidates and all(c.path.startswith(f"deliveries/replay_id={rid}/arrived=") for c in plan.candidates)
    assert plan.kept == [f"{replay.delivery_manifest_path(rid)} (manifest: kept)"]
    assert cleanup.run(conn, small_cfg, cleanup.Target("replay", rid), None)[0].token == plan.token
    assert conn.execute("SELECT count(*) FROM generator_meta.cleanups").fetchone() == (0,)


def test_delete_replay_copies_exactly(setup: tuple[psycopg.Connection, str, str, str], small_cfg: config.Config,
                                      data_root: Path) -> None:
    conn, _, run_id, rid = setup
    unknown = data_root / f"deliveries/replay_id={rid}/notes.txt"  # not recorded: never a candidate
    unknown.write_text("mine\n")
    before = tree(data_root)
    plan, _ = cleanup.run(conn, small_cfg, cleanup.Target("replay", rid), None)
    _, result = cleanup.run(conn, small_cfg, cleanup.Target("replay", rid), plan.token)
    assert result is not None and result.deleted == len(plan.candidates) and result.already_gone == 0
    after = tree(data_root)
    assert set(before) - set(after) == {c.path for c in plan.candidates}
    assert set(after) - set(before) == {result.audit_path}
    assert unknown.read_text() == "mine\n" and (data_root / replay.delivery_manifest_path(rid)).is_file()
    audit = json.loads((data_root / str(result.audit_path)).read_text())
    assert audit["cleanup_id"] == result.cleanup_id and audit["token"] == plan.token
    assert [d["path"] for d in audit["deleted_files"]] == sorted(c.path for c in plan.candidates)
    assert audit["deleted_bytes"] == result.bytes == sum(c.bytes for c in plan.candidates)
    marked = conn.execute("SELECT count(*) FROM generator_meta.file_deliveries WHERE deleted_by = %s",
                          (result.cleanup_id,)).fetchone()
    assert marked == (len(plan.candidates),)
    assert conn.execute("SELECT state FROM generator_meta.cleanups").fetchone() == ("COMPLETED",)
    # the same command again: nothing left; the replay can't be verified any more, and says why
    again, _ = cleanup.run(conn, small_cfg, cleanup.Target("replay", rid), None)
    assert again.candidates == []
    with pytest.raises(replay.ReplayError, match="removed by cleanup"):
        replay.replay(conn, small_cfg, PARAMS)


def test_wrong_or_stale_token_is_refused(setup: tuple[psycopg.Connection, str, str, str], small_cfg: config.Config,
                                         data_root: Path) -> None:
    conn, _, run_id, rid = setup
    plan, _ = cleanup.run(conn, small_cfg, cleanup.Target("replay", rid), None)
    before = tree(data_root)
    for bad in ("000000000000", cleanup.run(conn, small_cfg, cleanup.Target("run", run_id), None)[0].token):
        with pytest.raises(cleanup.CleanupError, match="does not match"):
            cleanup.run(conn, small_cfg, cleanup.Target("replay", rid), bad)
    # the recorded state changes after the dry run (here: one recorded size): the old token is stale
    conn.execute("UPDATE generator_meta.file_deliveries SET bytes = bytes + 1 WHERE delivered_path = %s",
                 (plan.candidates[0].path,))
    with pytest.raises(cleanup.CleanupError, match="does not match"):
        cleanup.run(conn, small_cfg, cleanup.Target("replay", rid), plan.token)
    assert tree(data_root) == before


def test_run_cleanup_keeps_manifest_and_validate_reports_it(setup: tuple[psycopg.Connection, str, str, str],
                                                            small_cfg: config.Config, data_root: Path) -> None:
    conn, _, run_id, rid = setup
    plan, _ = cleanup.run(conn, small_cfg, cleanup.Target("run", run_id), None)
    assert plan.kept == [f"{feeds.manifest_path(run_id)} (manifest: kept)"]
    _, result = cleanup.run(conn, small_cfg, cleanup.Target("run", run_id), plan.token)
    assert result is not None and result.deleted == len(plan.candidates)
    assert (data_root / feeds.manifest_path(run_id)).is_file()
    bad = {r.check.name: r for r in validate.validate(conn, small_cfg)[1] if not r.ok}
    assert bad["files_match_ledger"].violations == len(plan.candidates)
    assert all(f"deleted by cleanup {result.cleanup_id}" in s for s in bad["files_match_ledger"].samples)
    with pytest.raises(replay.ReplayError, match="deleted by cleanup"):  # originals gone: no new replay
        replay.replay(conn, small_cfg, replay.Params(date(2026, 1, 9), date(2026, 1, 10), ("clickstream",), 0, 0, 0))
    assert replay.replay(conn, small_cfg, PARAMS).verified_only  # earlier copies are independent of originals


def test_changed_files_and_symlinks_are_not_deleted(setup: tuple[psycopg.Connection, str, str, str],
                                                    small_cfg: config.Config, data_root: Path,
                                                    tmp_path: Path) -> None:
    conn, _, run_id, rid = setup
    plan, _ = cleanup.run(conn, small_cfg, cleanup.Target("replay", rid), None)
    changed = data_root / plan.candidates[0].path
    changed.write_text("edited by someone\n")
    _, result = cleanup.run(conn, small_cfg, cleanup.Target("replay", rid), plan.token)
    assert result is not None and result.kept_changed == [plan.candidates[0].path]
    assert changed.read_text() == "edited by someone\n"
    audit = json.loads((data_root / str(result.audit_path)).read_text())
    assert audit["kept_changed"] == [plan.candidates[0].path]
    # a symlink swapped in for a recorded file: refused, and the link target survives
    plan, _ = cleanup.run(conn, small_cfg, cleanup.Target("run", run_id), None)
    victim = data_root / plan.candidates[0].path
    outside = tmp_path / "precious.csv"
    outside.write_bytes(victim.read_bytes())
    victim.unlink()
    victim.symlink_to(outside)
    with pytest.raises(feeds.FeedError, match="symlink"):
        cleanup.run(conn, small_cfg, cleanup.Target("run", run_id), plan.token)
    assert outside.is_file()


def test_unsafe_roots_and_states_are_refused(setup: tuple[psycopg.Connection, str, str, str],
                                             small_cfg: config.Config, data_root: Path, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    conn, _, run_id, rid = setup
    (data_root / ".git").mkdir()
    with pytest.raises(cleanup.CleanupError, match=".git"):
        cleanup.run(conn, small_cfg, cleanup.Target("run", run_id), None)
    (data_root / ".git").rmdir()
    other = tmp_path / "other-root"
    other.mkdir()
    monkeypatch.setenv("ECOMGEN_DATA_ROOT", str(other))
    with pytest.raises(cleanup.CleanupError, match="not the current data root"):
        cleanup.run(conn, small_cfg, cleanup.Target("run", run_id), None)
    monkeypatch.setenv("ECOMGEN_DATA_ROOT", str(data_root))
    conn.execute("UPDATE generator_meta.replays SET state = 'RUNNING', finished_at = NULL")
    with pytest.raises(cleanup.CleanupError, match="is RUNNING"):
        cleanup.run(conn, small_cfg, cleanup.Target("replay", rid), None)
    for missing in (cleanup.Target("run", "run_20990101T000000Z_000000"),
                    cleanup.Target("replay", "rpl_000000000000")):
        with pytest.raises(db.RunLookupError, match="not found"):
            cleanup.run(conn, small_cfg, missing, None)


def test_refuses_while_the_run_lock_is_held(setup: tuple[psycopg.Connection, str, str, str],
                                            small_cfg: config.Config) -> None:
    conn, url, run_id, rid = setup
    with psycopg.connect(url, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (load._RUN_LOCK_ID,))
        with pytest.raises(load.LoadError, match="holds the run lock"):
            cleanup.run(conn, small_cfg, cleanup.Target("replay", rid), None)


def test_staging_only_known_directories(setup: tuple[psycopg.Connection, str, str, str], small_cfg: config.Config,
                                        data_root: Path) -> None:
    conn, _, run_id, rid = setup
    for name in (run_id, rid, "rpl_ffffffffffff", "someone-elses"):
        d = data_root / "staging" / name
        d.mkdir(parents=True)
        (d / "x.csv").write_text("leftover\n")
    plan, _ = cleanup.run(conn, small_cfg, cleanup.Target("staging"), None)
    assert [c.path for c in plan.candidates] == sorted([f"staging/{run_id}", f"staging/{rid}"])
    assert sorted(plan.kept) == ["staging/rpl_ffffffffffff (unknown to this database: skipped)",
                                 "staging/someone-elses (unknown to this database: skipped)"]
    _, result = cleanup.run(conn, small_cfg, cleanup.Target("staging"), plan.token)
    assert result is not None
    assert sorted(p.name for p in (data_root / "staging").iterdir()) == ["rpl_ffffffffffff", "someone-elses"]
    audit = json.loads((data_root / str(result.audit_path)).read_text())
    assert audit["staging_dirs"] == [c.path for c in plan.candidates] and audit["deleted_files"] == []


def test_sigkill_mid_delete_then_rerun(setup: tuple[psycopg.Connection, str, str, str], cfg_path: Path,
                                       small_cfg: config.Config, data_root: Path) -> None:
    conn, url, run_id, rid = setup
    plan, _ = cleanup.run(conn, small_cfg, cleanup.Target("replay", rid), None)
    proc = subprocess.run([sys.executable, "-c", CRASH_IN_CLEANUP, str(cfg_path), url, rid, "3", plan.token],
                          capture_output=True, timeout=120)
    assert proc.returncode == -signal.SIGKILL, proc.stderr.decode()
    first = conn.execute("SELECT cleanup_id, state FROM generator_meta.cleanups").fetchone()
    assert first is not None and first[1] == "RUNNING"
    marked = conn.execute("SELECT count(*) FROM generator_meta.file_deliveries"
                          " WHERE deleted_by IS NOT NULL").fetchone()
    assert marked == (2,)  # the 3rd file is unlinked but not yet marked
    plan2, _ = cleanup.run(conn, small_cfg, cleanup.Target("replay", rid), None)
    assert len(plan2.candidates) == len(plan.candidates) - 2 and plan2.token != plan.token
    _, result = cleanup.run(conn, small_cfg, cleanup.Target("replay", rid), plan2.token)
    assert result is not None and result.already_gone == 1 and result.deleted == len(plan.candidates) - 3
    states: dict[str, str] = {r[0]: r[1] for r in conn.execute("SELECT cleanup_id, state FROM generator_meta.cleanups")}
    assert states == {first[0]: "COMPLETED", result.cleanup_id: "COMPLETED"}  # the interrupted one's audit finished
    assert (data_root / cleanup.audit_path(first[0])).is_file()
    unmarked = conn.execute("SELECT count(*) FROM generator_meta.file_deliveries WHERE replay_id = %s"
                            " AND original_path IS NOT NULL AND deleted_at IS NULL", (rid,)).fetchone()
    assert unmarked == (0,)  # ledger == disk
    assert not [p for p in tree(data_root) if p.startswith(f"deliveries/replay_id={rid}/arrived=")]


def test_cli_cleanup(db_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("ECOMMERCE_DATABASE_URL", db_url)
    cfg = str(EXAMPLE)
    assert main(["cleanup-files", cfg, "--staging"]) == 3  # not migrated
    assert main(["init-db", cfg]) == 0 and main(["generate", cfg, "--profile", "smoke"]) == 0
    run_id = [line.split()[1] for line in capsys.readouterr().out.splitlines() if line.startswith("run ")][0]
    assert main(["cleanup-files", cfg, "--run-id", run_id]) == 0
    out = capsys.readouterr().out
    assert "dry run:" in out and "keep   manifests/" in out
    tok = out.rsplit("--confirm ", 1)[1].strip()
    assert main(["cleanup-files", cfg, "--run-id", run_id, "--delete"]) == 2  # --delete needs --confirm
    assert main(["cleanup-files", cfg, "--run-id", run_id, "--delete", "--confirm", "000000000000"]) == 1
    assert main(["cleanup-files", cfg, "--run-id", run_id, "--delete", "--confirm", tok]) == 0
    assert "deleted" in capsys.readouterr().out
    assert main(["cleanup-files", cfg, "--run-id", "run_20990101T000000Z_000000"]) == 3
    with pytest.raises(SystemExit, match="2"):
        main(["cleanup-files", cfg])  # a target is required (argparse)
