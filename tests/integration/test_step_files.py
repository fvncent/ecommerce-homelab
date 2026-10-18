"""Files per clock step (`advance` with feeds on) against a real PostgreSQL: every step publishes its window's
events with a manifest; the chain's files together equal a one-shot run's, and agree with the database at every
step; real SIGKILLs at each stage of the protocol resume cleanly. Fixtures in conftest.py."""

import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from ecomgen import cleanup, config, feeds, generate, load, replay, validate

ADMIN_URL = os.environ.get("ECOMGEN_TEST_DATABASE_URL", "")
EXAMPLE = Path(__file__).parents[2] / "config" / "generator.example.yaml"
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="ECOMGEN_TEST_DATABASE_URL not set")

Migrated = Callable[..., psycopg.Connection]
Digest = Callable[[psycopg.Connection], dict[str, str]]
START, END = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
MID = START + timedelta(days=12)

# `advance --to argv[3]` in a child process that SIGKILLs itself at the argv[5]-th call of a stage (argv[4]):
# db = writing a step chunk, csv / jsonl = staging settlement / clickstream records, place = right after a file is
# in place but before its ledger row, manifest = building the step manifest, complete = before COMPLETED.
CRASH = """
import os, signal, sys, psycopg
from datetime import datetime
from ecomgen import config, feeds, generate, load
stage, nth = sys.argv[4], int(sys.argv[5])
mod, name = {"db": (generate, "step_chunk"), "csv": (feeds, "_add_settlements"), "jsonl": (feeds, "_add_events"),
             "place": (feeds, "place_file"), "manifest": (feeds, "manifest"), "complete": (load, "_complete")}[stage]
real, calls = getattr(mod, name), [0]
def crash(*args, **kwargs):
    calls[0] += 1
    if calls[0] == nth and stage != "place":
        os.kill(os.getpid(), signal.SIGKILL)
    result = real(*args, **kwargs)
    if calls[0] == nth:
        os.kill(os.getpid(), signal.SIGKILL)
    return result
setattr(mod, name, crash)
with psycopg.connect(sys.argv[2], autocommit=True) as conn:
    load.advance(conn, config.load(sys.argv[1]), datetime.fromisoformat(sys.argv[3]))
"""


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    text = EXAMPLE.read_text()
    for old, new in (("chunk_rows: 5000", "chunk_rows: 200"), ("customers: 5000", "customers: 300"),
                     ("products: 2000", "products: 150"), ("sellers: 100", "sellers: 20"),
                     ("orders: 10000", "orders: 900"), ("clickstream_events: 100000", "clickstream_events: 700")):
        assert old in text
        text = text.replace(old, new)
    path = tmp_path / "stepped.yaml"
    path.write_text(text)
    return path


@pytest.fixture
def small_cfg(cfg_path: Path) -> config.Config:
    return config.load(cfg_path)


def records(root: Path) -> dict[str, list[str]]:
    """feed directory -> sorted record lines of every feed file under root (CSV headers dropped)."""
    out: dict[str, list[str]] = {"source_feeds": [], "clickstream": []}
    for top in out:
        for path in (root / top).rglob("*"):
            if path.is_file():
                lines = path.read_text().splitlines()
                out[top] += lines[1:] if path.suffix == ".csv" else lines
    return {k: sorted(v) for k, v in out.items()}


@pytest.fixture
def one_shot(migrated: Migrated, small_cfg: config.Config, digest: Digest, tmp_path: Path,
             monkeypatch: pytest.MonkeyPatch, data_root: Path) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Database digest and file records of `generate` with the same config, in its own data root."""
    root = tmp_path / "one_shot"
    root.mkdir()
    monkeypatch.setenv("ECOMGEN_DATA_ROOT", str(root))
    c = migrated(small_cfg)
    load.run(c, small_cfg)
    monkeypatch.setenv("ECOMGEN_DATA_ROOT", str(data_root))
    return digest(c), records(root)


def failing(conn: psycopg.Connection, cfg: config.Config, run_id: str | None = None) -> list[str]:
    _, results = validate.validate(conn, cfg, run_id)
    return [f"{r.check.name}: {r.samples}" for r in results if not r.ok]


def ledger(conn: psycopg.Connection, run_id: str | None = None) -> dict[str, str]:
    rows = conn.execute("SELECT rel_path, sha256 FROM generator_meta.published_files"
                        + (" WHERE run_id = %s" if run_id else ""), (run_id,) if run_id else ()).fetchall()
    return {r[0]: r[1] for r in rows}


def on_disk(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for top in ("source_feeds", "clickstream", "manifests")
            for p in (root / top).rglob("*") if p.is_file()}


def assert_converged(conn: psycopg.Connection, cfg: config.Config, root: Path, digest: Digest,
                     one_shot: tuple[dict[str, str], dict[str, list[str]]]) -> None:
    """Chain at the end: rows and file records equal the one-shot run's; ledger == disk; no staging left."""
    assert failing(conn, cfg) == []
    assert digest(conn) == one_shot[0]
    assert records(root) == one_shot[1]
    assert on_disk(root) == set(ledger(conn))
    assert not any((root / "staging").glob("*/*")) if (root / "staging").exists() else True


def test_multi_step_chain(migrated: Migrated, small_cfg: config.Config, data_root: Path, digest: Digest,
                          one_shot: tuple[dict[str, str], dict[str, list[str]]]) -> None:
    c = migrated(small_cfg)
    stops = [START, START + timedelta(days=3), START + timedelta(days=7, hours=12), MID, END]
    previous = None
    first_id = None
    for as_of in stops:
        r = load.advance(c, small_cfg, as_of)
        assert r is not None
        first_id = first_id or r.run_id
        assert failing(c, small_cfg) == [], as_of  # files and rows agree after every step
        doc = json.loads((data_root / feeds.manifest_path(r.run_id)).read_text())
        assert doc["run_id"] == r.run_id and doc["clock_step"] == {
            "first_step_run_id": first_id, "previous_as_of": feeds.ts(previous) if previous else None,
            "as_of": feeds.ts(as_of), "window": "(previous_as_of, as_of]" if previous else "[clock_start, as_of]",
            "window_start": feeds.ts(previous or START)}
        assert {f["file_path"] for f in doc["files"]} == set(ledger(c, r.run_id)) - {feeds.manifest_path(r.run_id)}
        plan = generate.plan_step(generate.World.from_config(small_cfg), previous, as_of)
        for f in doc["files"]:
            assert f["file_path"].rsplit("/", 1)[1].startswith(r.run_id)
            for t in (f["start_event_time"], f["end_event_time"]):  # every record inside the step's window
                assert feeds.in_window(plan, datetime.fromisoformat(t))
        assert r.files.files == len(doc["files"]) + 1 and (previous is None or r.files.records)
        previous = as_of
    assert_converged(c, small_cfg, data_root, digest, one_shot)


def test_same_steps_give_the_same_files(migrated: Migrated, small_cfg: config.Config, data_root: Path,
                                        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    per_step = []
    for k in range(2):
        root = tmp_path / f"chain{k}"
        root.mkdir()
        monkeypatch.setenv("ECOMGEN_DATA_ROOT", str(root))
        c = migrated(small_cfg)
        steps = []
        for as_of in (START + timedelta(days=4), MID, END):
            r = load.advance(c, small_cfg, as_of)
            assert r is not None
            steps.append({rel.replace(r.run_id, "RUN"): (root / rel).read_bytes()
                          for rel in ledger(c, r.run_id) if not rel.startswith("manifests/")})
        per_step.append(steps)
    assert per_step[0] == per_step[1]  # byte-identical files apart from the step's run id in their names


@pytest.mark.parametrize("stage, nth", [("db", 2), ("csv", 2), ("jsonl", 3), ("place", 3), ("manifest", 1),
                                        ("complete", 1)])
def test_sigkill_at_each_stage_then_resume(migrated: Migrated, db_url: str, cfg_path: Path,
                                           small_cfg: config.Config, data_root: Path, digest: Digest,
                                           one_shot: tuple[dict[str, str], dict[str, list[str]]],
                                           stage: str, nth: int) -> None:
    c = migrated(small_cfg, db_url)
    load.advance(c, small_cfg, MID)
    proc = subprocess.run([sys.executable, "-c", CRASH, str(cfg_path), db_url, END.isoformat(), stage, str(nth)],
                          capture_output=True, timeout=120)
    assert proc.returncode == -signal.SIGKILL, proc.stderr.decode()
    st = load.status(c, small_cfg)
    assert st.state == "RUNNING" and st.as_of == END  # never marked COMPLETED early
    assert "run_completed: " + str([f"{st.run_id} is RUNNING"]) in failing(c, small_cfg)
    with pytest.raises(load.LoadError, match="finish it with `ecomgen resume`"):
        load.advance(c, small_cfg, END)
    r = load.resume(c, small_cfg)
    assert r is not None and r.as_of == END
    assert load.status(c, small_cfg).state == "COMPLETED"
    assert_converged(c, small_cfg, data_root, digest, one_shot)
    assert load.resume(c, small_cfg) is None


def test_repeating_a_completed_step_changes_no_file(migrated: Migrated, small_cfg: config.Config,
                                                    data_root: Path) -> None:
    c = migrated(small_cfg)
    for as_of in (MID, END):
        load.advance(c, small_cfg, as_of)
    before = {rel: (data_root / rel).stat().st_mtime_ns for rel in on_disk(data_root)}
    for as_of in (MID, END):
        assert load.advance(c, small_cfg, as_of) is None
    assert load.resume(c, small_cfg) is None
    assert {rel: (data_root / rel).stat().st_mtime_ns for rel in on_disk(data_root)} == before


@pytest.mark.parametrize("damage", ["delete", "corrupt"])
def test_missing_or_corrupted_step_file_is_reported(migrated: Migrated, small_cfg: config.Config, data_root: Path,
                                                    damage: str) -> None:
    c = migrated(small_cfg)
    first = load.advance(c, small_cfg, MID)
    load.advance(c, small_cfg, END)
    assert first is not None
    rel = sorted(r for r in ledger(c, first.run_id) if r.startswith("clickstream/"))[0]
    if damage == "delete":
        (data_root / rel).unlink()
    else:
        (data_root / rel).write_text((data_root / rel).read_text().replace("evt_", "evx_", 1))
    bad = failing(c, small_cfg)
    assert any(b.startswith("files_match_ledger") and rel in b for b in bad)
    assert any(b.startswith("events_unique_and_complete") or b.startswith("orders_placed_once") for b in bad)


def test_unwritable_data_root_fails_the_step_and_resume_finishes(
        migrated: Migrated, small_cfg: config.Config, data_root: Path, digest: Digest,
        one_shot: tuple[dict[str, str], dict[str, list[str]]]) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    c = migrated(small_cfg)
    load.advance(c, small_cfg, MID)
    dirs = [data_root, *(p for p in data_root.rglob("*") if p.is_dir())]
    for d in dirs:  # the whole tree read-only, as after a remount or a wrong chown
        d.chmod(0o555)
    try:
        with pytest.raises(OSError):
            load.advance(c, small_cfg, END)
        st = load.status(c, small_cfg)
        assert st.state == "FAILED" and "Permission" in (st.error or "")
    finally:
        for d in dirs:
            d.chmod(0o755)
    assert load.resume(c, small_cfg) is not None
    assert_converged(c, small_cfg, data_root, digest, one_shot)


def test_database_error_while_publishing_fails_the_step_and_resume_finishes(
        migrated: Migrated, small_cfg: config.Config, data_root: Path, digest: Digest,
        one_shot: tuple[dict[str, str], dict[str, list[str]]], monkeypatch: pytest.MonkeyPatch) -> None:
    c = migrated(small_cfg)
    load.advance(c, small_cfg, MID)

    def broken(*args: object) -> dict[str, object]:
        raise psycopg.errors.QueryCanceled("canceling statement due to user request")
    real = feeds.manifest
    monkeypatch.setattr(feeds, "manifest", broken)
    with pytest.raises(psycopg.errors.QueryCanceled):
        load.advance(c, small_cfg, END)
    assert load.status(c, small_cfg).state == "FAILED"
    monkeypatch.setattr(feeds, "manifest", real)
    assert load.resume(c, small_cfg) is not None
    assert_converged(c, small_cfg, data_root, digest, one_shot)


def test_chain_keeps_its_data_root_and_feeds(migrated: Migrated, small_cfg: config.Config, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    c = migrated(small_cfg)
    load.advance(c, small_cfg, MID)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("ECOMGEN_DATA_ROOT", str(elsewhere))
    with pytest.raises(load.LoadError, match="data_root differ"):
        load.advance(c, small_cfg, END)


def test_replay_and_cleanup_of_a_step(migrated: Migrated, small_cfg: config.Config, data_root: Path) -> None:
    c = migrated(small_cfg)
    first = load.advance(c, small_cfg, MID)
    load.advance(c, small_cfg, END)
    assert first is not None
    params = replay.Params(START.date() + timedelta(days=2), START.date() + timedelta(days=5),
                           ("clickstream", "payment_settlement"), 24, 0, 0)
    r = replay.replay(c, small_cfg, params, first.run_id)
    assert r.deliveries > 0 and r.newly_delivered > 0
    plan = cleanup.plan(c, small_cfg, cleanup.Target("replay", r.replay_id))
    assert plan.candidates and all(p.path.startswith(f"deliveries/replay_id={r.replay_id}/") for p in plan.candidates)
    files = cleanup.plan(c, small_cfg, cleanup.Target("run", first.run_id))
    assert {p.path for p in files.candidates} == set(ledger(c, first.run_id)) - {feeds.manifest_path(first.run_id)}
