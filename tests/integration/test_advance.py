"""`ecomgen advance` (clock steps) against a real PostgreSQL, including a real SIGKILL mid-step. Fixtures in
conftest.py; the pure step logic is in tests/test_clock.py."""

import os
import signal
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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
START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2026, 2, 1, tzinfo=UTC)

# Runs `advance --to` in a child process that SIGKILLs itself just before writing step chunk 1: the step is left
# RUNNING with chunk 0 committed, exactly as after a power cut.
CRASH = """
import os, signal, sys, psycopg
from datetime import datetime
from ecomgen import config, generate, load
real = generate.step_chunk
def crash(world, previous, as_of, chunk_no, chunk_rows):
    if chunk_no == 1:
        os.kill(os.getpid(), signal.SIGKILL)
    return real(world, previous, as_of, chunk_no, chunk_rows)
generate.step_chunk = crash
with psycopg.connect(sys.argv[2], autocommit=True) as conn:
    load.advance(conn, config.load(sys.argv[1]), datetime.fromisoformat(sys.argv[3]))
"""


def _small(text: str, feeds: bool = False) -> str:
    for old, new in (("chunk_rows: 5000", "chunk_rows: 200"), ("customers: 5000", "customers: 300"),
                     ("products: 2000", "products: 150"), ("sellers: 100", "sellers: 20"),
                     ("orders: 10000", "orders: 900"), ("clickstream_events: 100000", "clickstream_events: 500")):
        assert old in text
        text = text.replace(old, new)
    if not feeds:
        text = text.replace("  csv: true", "  csv: false").replace("  jsonl: true", "  jsonl: false")
    return text


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    path = tmp_path / "stepped.yaml"
    path.write_text(_small(EXAMPLE.read_text()))
    return path


@pytest.fixture
def small_cfg(cfg_path: Path) -> config.Config:
    cfg = config.load(cfg_path)
    assert not cfg.filesystem.csv and not cfg.filesystem.jsonl
    return cfg


@pytest.fixture
def expected(migrated: Migrated, small_cfg: config.Config, digest: Digest) -> dict[str, str]:
    clean = migrated(small_cfg)
    load.run(clean, small_cfg)
    return digest(clean)


def days(*ds: float) -> list[datetime]:
    return [START + timedelta(days=d) for d in ds]


def ok(conn: psycopg.Connection, cfg: config.Config, run_id: str | None = None) -> list[str]:
    _, results = validate.validate(conn, cfg, run_id)
    return [f"{r.check.name}: {r.samples}" for r in results if not r.ok]


@pytest.mark.parametrize("stops", [days(31), days(*range(1, 32)), days(0, 0.5, 9.25, 20, 31)])
def test_steps_to_the_end_equal_generate(migrated: Migrated, small_cfg: config.Config, digest: Digest,
                                         expected: dict[str, str], stops: list[datetime]) -> None:
    c = migrated(small_cfg)
    updated = 0
    for as_of in stops:
        r = load.advance(c, small_cfg, as_of)
        assert r is not None and r.as_of == as_of and ok(c, small_cfg) == []
        updated += sum(r.updated.values())
    assert digest(c) == expected
    assert updated > 0 or len(stops) == 1
    assert load.advance(c, small_cfg, END) is None  # already there: idempotent


def test_each_step_holds_the_world_as_of_its_time_and_is_recorded(migrated: Migrated, small_cfg: config.Config) -> None:
    c = migrated(small_cfg)
    world = generate.World.from_config(small_cfg)
    previous = None
    for as_of in days(3, 3.5, 12):
        r = load.advance(c, small_cfg, as_of)
        assert r is not None and r.previous_as_of == previous
        n = world.orders_by(as_of)
        statuses: dict[str, int] = dict(c.execute("SELECT status, count(*) FROM commerce.orders GROUP BY 1").fetchall())
        want: dict[str, int] = {}
        for k in range(1, n + 1):
            s = world.order(k, as_of)[0].status
            want[s] = want.get(s, 0) + 1
        assert statuses == want and sum(want.values()) == n
        st = load.status(c, small_cfg)
        assert (st.run_id, st.state, st.previous_as_of, st.as_of) == (r.run_id, "COMPLETED", previous, as_of)
        assert ok(c, small_cfg, r.run_id) == []
        previous = as_of
    # orders were updated in place: same IDs, changed status (CDC sees UPDATEs, not delete + insert)
    later = c.execute("SELECT sum((table_rows->>'orders_updated')::int) FROM generator_meta.generation_chunks"
                      " WHERE table_rows ? 'orders_updated'").fetchone()
    assert later is not None and later[0] > 0


def test_refusals(migrated: Migrated, small_cfg: config.Config, tmp_path: Path,
                  monkeypatch: pytest.MonkeyPatch) -> None:
    one_shot = migrated(small_cfg)
    load.run(one_shot, small_cfg)
    with pytest.raises(load.LoadError, match="holds generate run"):
        load.advance(one_shot, small_cfg, END)

    c = migrated(small_cfg)
    assert load.advance(c, small_cfg, days(5)[0]) is not None
    with pytest.raises(load.LoadError, match="only moves forward"):
        load.advance(c, small_cfg, days(4)[0])
    with pytest.raises(load.LoadError, match="already holds run"):
        load.run(c, small_cfg)
    with pytest.raises(generate.GenerationError, match="outside"):
        load.advance(c, small_cfg, END + timedelta(seconds=1))
    other = tmp_path / "other.yaml"
    other.write_text(_small(EXAMPLE.read_text()).replace("seed: 20261017", "seed: 1"))
    with pytest.raises(load.LoadError, match="world differ"):
        load.advance(c, config.load(other), END)
    with_files = tmp_path / "files.yaml"
    with_files.write_text(_small(EXAMPLE.read_text(), feeds=True))
    with pytest.raises(load.LoadError, match="database rows only"):
        load.advance(c, config.load(with_files), END)
    monkeypatch.setattr(db, "has_column", lambda *a: False)
    with pytest.raises(load.LoadError, match="migration 0008"):
        load.advance(c, small_cfg, END)


def test_a_row_changed_outside_the_generator_stops_the_step(migrated: Migrated, small_cfg: config.Config,
                                                           digest: Digest, expected: dict[str, str]) -> None:
    c = migrated(small_cfg)
    load.advance(c, small_cfg, days(10)[0])
    world = generate.World.from_config(small_cfg)
    # an order that the next step must update (it is still open at day 10)
    n = next(k for k in range(world.orders_by(days(10)[0]), 0, -1)
             if world.order(k, days(10)[0])[0].status != world.order(k)[0].status)
    oid = f"ord_{n}"
    row = c.execute("SELECT status, updated_at FROM commerce.orders WHERE order_id = %s", (oid,)).fetchone()
    assert row is not None
    c.execute("UPDATE commerce.orders SET status = 'CANCELLED' WHERE order_id = %s", (oid,))
    with pytest.raises(load.LoadError, match="not as the previous step left them"):
        load.advance(c, small_cfg, END)
    st = load.status(c, small_cfg)
    assert st.state == "FAILED" and "not as the previous step" in (st.error or "")
    # the chunk holding the edited order was rolled back as a whole: the edit is still there, untouched
    assert c.execute("SELECT status, updated_at FROM commerce.orders WHERE order_id = %s",
                     (oid,)).fetchone() == ("CANCELLED", row[1])
    c.execute("UPDATE commerce.orders SET status = %s WHERE order_id = %s", (row[0], oid))
    assert load.resume(c, small_cfg) is not None
    assert digest(c) == expected and ok(c, small_cfg) == []


def test_resume_after_sigkill_mid_step(migrated: Migrated, db_url: str, cfg_path: Path, small_cfg: config.Config,
                                       digest: Digest, expected: dict[str, str]) -> None:
    c = migrated(small_cfg, db_url)
    load.advance(c, small_cfg, days(8)[0])
    proc = subprocess.run([sys.executable, "-c", CRASH, str(cfg_path), db_url, END.isoformat()],
                          capture_output=True, timeout=120)
    assert proc.returncode == -signal.SIGKILL, proc.stderr.decode()
    st = load.status(c, small_cfg)
    assert st.state == "RUNNING" and st.as_of == END and st.writer_pid is None
    assert st.progress["orders"][0] == small_cfg.generator.chunk_rows < st.progress["orders"][1]  # chunk 0 only
    assert ok(c, small_cfg) != []  # run_completed / ledger checks fail while it is unfinished
    with pytest.raises(load.LoadError, match="finish it with `ecomgen resume`"):
        load.advance(c, small_cfg, END)
    r = load.resume(c, small_cfg)
    assert r is not None and r.chunks_skipped == 1 and r.as_of == END and r.previous_as_of == days(8)[0]
    assert digest(c) == expected and ok(c, small_cfg) == []
    assert load.resume(c, small_cfg) is None


def test_chain_shape_is_enforced_by_the_schema(migrated: Migrated, small_cfg: config.Config) -> None:
    c = migrated(small_cfg)
    load.advance(c, small_cfg, days(2)[0])
    load.advance(c, small_cfg, days(4)[0])
    insert = ("INSERT INTO generator_meta.generation_runs (run_id, generator_version, seed, config_sha256, profile,"
              " state, finished_at, previous_as_of, as_of) VALUES (%s, 'x', 1, repeat('0', 64), 'mvp', 'COMPLETED',"
              " now(), %s, %s)")
    for previous, as_of in ((None, days(9)[0]),           # a second first step
                            (days(2)[0], days(9)[0]),     # a fork from an earlier step
                            (days(7)[0], days(9)[0]),     # from a time no step reached
                            (days(4)[0], days(3)[0]),     # backwards
                            (days(4)[0], days(2)[0])):    # an as_of used twice
        with pytest.raises(psycopg.errors.IntegrityError):
            with c.transaction():
                c.execute(insert, ("run_bad", previous, as_of))


def test_cli(cfg_path: Path, db_url: str, small_cfg: config.Config, migrated: Migrated,
             monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    migrated(small_cfg, db_url)
    monkeypatch.setenv("ECOMMERCE_DATABASE_URL", db_url)
    assert main(["advance", str(cfg_path), "--to", "2026-01-10T00:00:00Z"]) == 0
    out = capsys.readouterr().out
    assert "clock start -> 2026-01-10T00:00:00+00:00" in out and "inserted sellers=20" in out
    assert main(["advance", str(cfg_path), "--to", "2026-01-10T09:00:00+09:00"]) == 0  # same instant
    assert "already" in capsys.readouterr().out
    assert main(["advance", str(cfg_path), "--to", "2026-01-12T00:00:00"]) == 2  # no time zone
    assert main(["advance", str(cfg_path), "--to", "next week"]) == 2
    assert main(["advance", str(cfg_path), "--to", "2026-01-05T00:00:00Z"]) == 1
    assert "only moves forward" in capsys.readouterr().err
    assert main(["advance", str(cfg_path), "--to", "end"]) == 0
    out = capsys.readouterr().out
    assert "2026-01-10T00:00:00+00:00 -> 2026-02-01T00:00:00+00:00" in out and "updated orders=" in out
    assert main(["status", str(cfg_path)]) == 0
    assert "clock step: 2026-01-10T00:00:00+00:00 -> 2026-02-01T00:00:00+00:00" in capsys.readouterr().out
    assert main(["validate", str(cfg_path)]) == 0
