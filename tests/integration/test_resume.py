"""`status` and `resume` against a real PostgreSQL, including a real SIGKILL mid-run. Fixtures in conftest.py."""

import dataclasses
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest

from ecomgen import config, db, generate, load, validate
from ecomgen.cli import main

ADMIN_URL = os.environ.get("ECOMGEN_TEST_DATABASE_URL", "")
EXAMPLE = Path(__file__).parents[2] / "config" / "generator.example.yaml"
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="ECOMGEN_TEST_DATABASE_URL not set")

Migrated = Callable[..., psycopg.Connection]
Digest = Callable[[psycopg.Connection], dict[str, str]]

# Runs generate in a child process that SIGKILLs itself just before writing orders chunk 2:
# no exception handler runs, so the run is left RUNNING exactly as after a power cut or OOM kill.
CRASH = """
import os, signal, sys, psycopg
from ecomgen import config, generate, load
real = generate.generate_chunk
def crash(world, entity, chunk_no, chunk_rows):
    if entity == "orders" and chunk_no == 2:
        os.kill(os.getpid(), signal.SIGKILL)
    return real(world, entity, chunk_no, chunk_rows)
generate.generate_chunk = crash
with psycopg.connect(sys.argv[2], autocommit=True) as conn:
    load.run(conn, config.load(sys.argv[1]), "smoke")
"""


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    # A real file: resume compares the run's config_sha256 with this file's hash.
    path = tmp_path / "generator.yaml"
    text = EXAMPLE.read_text()
    assert "chunk_rows: 5000" in text
    path.write_text(text.replace("chunk_rows: 5000", "chunk_rows: 300"))  # smoke orders span 4 chunks
    return path


@pytest.fixture
def small_cfg(cfg_path: Path) -> config.Config:
    return config.load(cfg_path)


def one(conn: psycopg.Connection, query: str) -> object:
    row = conn.execute(query.encode()).fetchone()
    assert row is not None
    return row[0]


def crash_run(cfg_path: Path, url: str) -> None:
    proc = subprocess.run([sys.executable, "-c", CRASH, str(cfg_path), url], capture_output=True, timeout=120)
    assert proc.returncode == -signal.SIGKILL, proc.stderr.decode()


@pytest.fixture
def expected(migrated: Migrated, small_cfg: config.Config, digest: Digest) -> dict[str, str]:
    clean = migrated(small_cfg)
    load.run(clean, small_cfg, "smoke")
    return digest(clean)


def test_resume_after_hard_crash(
    migrated: Migrated, db_url: str, cfg_path: Path, small_cfg: config.Config, digest: Digest,
    expected: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    c = migrated(small_cfg, db_url)
    crash_run(cfg_path, db_url)

    st = load.status(c, small_cfg)
    assert (st.state, st.writer_pid, st.error) == ("RUNNING", None, None)  # the dead session released the lock
    assert st.progress["orders"] == (600, 1000) and st.progress["products"] == (200, 200)
    monkeypatch.setenv("ECOMMERCE_DATABASE_URL", db_url)
    assert main(["status", str(cfg_path)]) == 0
    assert "the run crashed; run `ecomgen resume`" in capsys.readouterr().out
    with pytest.raises(load.LoadError, match=r"already holds run .* use `ecomgen resume`"):
        load.run(c, small_cfg, "smoke")  # generate still refuses and points to resume

    assert main(["resume", str(cfg_path)]) == 0
    assert "2 chunks written, 7 already committed" in capsys.readouterr().out
    assert digest(c) == expected  # identical to an uninterrupted run: no duplicates, nothing missing
    assert all(r.ok for r in validate.validate(c, small_cfg)[1])
    st = load.status(c, small_cfg)
    assert st.state == "COMPLETED" and st.finished_at is not None and st.progress["orders"] == (1000, 1000)
    assert main(["resume", str(cfg_path)]) == 0
    assert "already COMPLETED" in capsys.readouterr().out


def test_resume_after_failed_run(
    migrated: Migrated, small_cfg: config.Config, digest: Digest, expected: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = migrated(small_cfg)
    real = generate.generate_chunk

    def boom(world: generate.World, entity: str, chunk_no: int, chunk_rows: int) -> generate.Chunk:
        if entity == "orders" and chunk_no == 3:
            raise RuntimeError("simulated failure")
        return real(world, entity, chunk_no, chunk_rows)

    monkeypatch.setattr(generate, "generate_chunk", boom)
    with pytest.raises(RuntimeError):
        load.run(c, small_cfg, "smoke")
    st = load.status(c, small_cfg)
    assert st.state == "FAILED" and st.error == "RuntimeError: simulated failure"

    monkeypatch.setattr(generate, "generate_chunk", real)
    result = load.resume(c, small_cfg)
    assert result is not None and (result.chunks_written, result.chunks_skipped) == (1, 8)
    assert result.counts == {t: one(c, f"SELECT count(*) FROM commerce.{t}") for t in load.TABLES} | {
        load.OUTBOX: one(c, "SELECT count(*) FROM integration.outbox_events")}
    assert digest(c) == expected
    row = c.execute("SELECT state, error, finished_at IS NOT NULL FROM generator_meta.generation_runs").fetchone()
    assert row == ("COMPLETED", "RuntimeError: simulated failure", True)  # first failure reason kept


def test_resume_ignores_edits_that_cannot_change_rows(
    migrated: Migrated, db_url: str, cfg_path: Path, small_cfg: config.Config, digest: Digest,
    expected: dict[str, str],
) -> None:
    """The World is rebuilt from the run's record, so seed/clock/count edits, comments, non-row settings and a
    package version bump can't change a resumed run: it still matches an uninterrupted one."""
    c = migrated(small_cfg, db_url)
    crash_run(cfg_path, db_url)
    text = cfg_path.read_text()
    for old, new in [("seed: 20261017", "seed: 1"), ("clock_start: '2026-01-01T00:00:00Z'", "clock_start: '2025-06-01T00:00:00Z'"),
                     ("clock_end: '2026-02-01T00:00:00Z'", "clock_end: '2026-03-01T00:00:00Z'"),
                     ("orders: 10000", "orders: 77"), ("bulk_copy: true", "bulk_copy: false")]:
        assert old in text
        text = text.replace(old, new)
    cfg_path.write_text(text + "\n# a comment\n")
    c.execute("UPDATE generator_meta.generation_runs SET generator_version = '0.0.9'")
    edited = config.load(cfg_path)
    assert load.status(c, edited).resume_mismatch == []
    assert load.resume(c, edited) is not None
    assert digest(c) == expected


@pytest.mark.parametrize(
    ("edit", "field"),
    [
        (("chunk_rows: 300", "chunk_rows: 250"), "chunk_rows"),  # would shift chunk boundaries
        (("application_schema: commerce", "application_schema: shop"), "application_schema"),  # write target
    ],
)
def test_resume_refuses_changes_that_matter(
    migrated: Migrated, db_url: str, cfg_path: Path, small_cfg: config.Config, edit: tuple[str, str], field: str
) -> None:
    c = migrated(small_cfg, db_url)
    crash_run(cfg_path, db_url)
    text = cfg_path.read_text()
    assert edit[0] in text
    cfg_path.write_text(text.replace(*edit))
    changed = config.load(cfg_path)
    assert load.status(c, changed).resume_mismatch == [field]
    with pytest.raises(load.LoadError, match=rf"can't resume run .*: {field} differ"):
        load.resume(c, changed)
    assert one(c, "SELECT state FROM generator_meta.generation_runs") == "RUNNING"  # untouched


def test_recorded_fields_cover_every_world_field(migrated: Migrated, db_url: str, cfg_path: Path,
                                                 small_cfg: config.Config) -> None:
    # A future generation input must be a World field; this keeps it recorded (and so fixed across resume).
    c = migrated(small_cfg, db_url)
    crash_run(cfg_path, db_url)
    recorded = one(c, "SELECT resume_fields FROM generator_meta.generation_runs")
    assert isinstance(recorded, dict)
    assert set(recorded["world"]) == {f.name for f in dataclasses.fields(generate.World)}
    assert recorded["world"]["orders"] == load.PROFILE_COUNTS["smoke"]["orders"]  # preset, not config counts


def test_resume_refuses_other_data_version_or_bad_record(
    migrated: Migrated, db_url: str, cfg_path: Path, small_cfg: config.Config
) -> None:
    c = migrated(small_cfg, db_url)
    crash_run(cfg_path, db_url)
    c.execute("UPDATE generator_meta.generation_runs SET resume_fields = resume_fields || '{\"data_version\": 0}'")
    with pytest.raises(load.LoadError, match="data_version differ"):
        load.resume(c, small_cfg)
    c.execute("UPDATE generator_meta.generation_runs SET resume_fields = resume_fields"
              " || jsonb_build_object('data_version', %s::int)", (generate.DATA_VERSION,))
    c.execute("UPDATE generator_meta.generation_runs SET resume_fields = jsonb_set(resume_fields, '{world,orders}', '0')")
    with pytest.raises(load.LoadError, match="recorded world is malformed"):
        load.resume(c, small_cfg)
    c.execute("UPDATE generator_meta.generation_runs SET resume_fields = NULL")
    with pytest.raises(load.LoadError, match="predates migration 0003"):
        load.resume(c, small_cfg)
    with pytest.raises(psycopg.errors.CheckViolation):  # shape enforced in the schema (0004)
        c.execute("UPDATE generator_meta.generation_runs SET resume_fields = '[]'")


def test_status_survives_malformed_planned_counts(
    migrated: Migrated, db_url: str, cfg_path: Path, small_cfg: config.Config
) -> None:
    c = migrated(small_cfg, db_url)
    crash_run(cfg_path, db_url)
    c.execute("UPDATE generator_meta.generation_runs SET counts = jsonb_set(counts, '{planned}', '[]')")
    assert load.status(c, small_cfg).progress["orders"] == (600, 0)


def test_resume_refuses_while_a_writer_is_active(
    migrated: Migrated, db_url: str, cfg_path: Path, small_cfg: config.Config
) -> None:
    c = migrated(small_cfg, db_url)
    crash_run(cfg_path, db_url)
    with psycopg.connect(db_url, autocommit=True) as writer:
        writer.execute("SELECT pg_advisory_lock(%s)", (load._RUN_LOCK_ID,))  # stands in for a live generate
        pid = writer.info.backend_pid
        assert load.status(c, small_cfg).writer_pid == pid
        with pytest.raises(load.LoadError, match=rf"backend pid {pid}.*pg_terminate_backend\({pid}\)"):
            load.resume(c, small_cfg)
    assert load.status(c, small_cfg).writer_pid is None


def test_no_runs(
    migrated: Migrated, db_url: str, small_cfg: config.Config, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    c = migrated(small_cfg, db_url)
    with pytest.raises(db.RunLookupError, match="no runs recorded"):
        load.status(c, small_cfg)
    with pytest.raises(db.RunLookupError, match="no runs recorded"):
        load.resume(c, small_cfg)
    monkeypatch.setenv("ECOMMERCE_DATABASE_URL", db_url)
    assert main(["status", str(EXAMPLE)]) == 3
    assert main(["resume", str(EXAMPLE), "--run-id", "run_nope"]) == 3  # like status/validate
    assert "run run_nope not found" in capsys.readouterr().err
