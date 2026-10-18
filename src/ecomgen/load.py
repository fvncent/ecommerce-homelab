"""Write generated chunks to PostgreSQL with a run/chunk ledger.

Each chunk's rows and its generation_chunks marker commit in one transaction: a marker means the chunk is
durable; no marker means nothing from it was committed. Chunks already marked for a run are skipped, which
is what makes a re-run of the same run (resume) idempotent.
"""

import dataclasses
import json
import secrets
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from ecomgen import __version__, config, db, feeds, generate
from ecomgen.config import Config

# Built-in profile counts; other profiles take counts from the config file.
PROFILE_COUNTS = {"smoke": {"sellers": 20, "customers": 500, "products": 200, "orders": 1000,
                             "clickstream_events": 10_000}}
TABLES = ("sellers", "categories", "customers", "products", "orders", "order_items", "payment_attempts")
# Arbitrary constant; held for the whole run so two generate/resume processes never write concurrently.
_RUN_LOCK_ID = 7_302_410_002


class LoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunResult:
    run_id: str
    counts: dict[str, int]  # rows written per table, from the chunk ledger
    chunks_written: int
    chunks_skipped: int
    files: feeds.Published


def world_for(cfg: Config, preset: str | None) -> generate.World:
    """Config counts, or a built-in preset's counts when one is named explicitly (e.g. --profile smoke)."""
    world = generate.World.from_config(cfg)
    if preset is None:
        return world
    c = PROFILE_COUNTS[preset]
    return replace(world, sellers=c["sellers"], customers=c["customers"], products=c["products"], orders=c["orders"],
                   clickstream_events=c["clickstream_events"])


def _write(cur: psycopg.Cursor[tuple[object, ...]], cfg: Config, table: str,
           rows: Sequence[tuple[object, ...]]) -> None:
    fields: tuple[str, ...] = getattr(rows[0], "_fields")  # by column name, never positional
    target = sql.SQL("{}.{} ({})").format(
        sql.Identifier(cfg.postgres.application_schema), sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, fields)),
    )
    if cfg.postgres.bulk_copy:
        with cur.copy(sql.SQL("COPY {} FROM STDIN").format(target)) as copy:
            for row in rows:
                copy.write_row(row)
    else:
        values = sql.SQL(", ").join([sql.Placeholder()] * len(fields))
        cur.executemany(sql.SQL("INSERT INTO {} VALUES ({})").format(target, values), rows)


# Generation is a pure function of the World, DATA_VERSION and chunk_rows. Resume rebuilds the World from what the
# run recorded (so config edits to seed/clock/counts can't change it) and requires the rest to match, including
# where and how its files are published (a different root or layout would publish a second, different set).
RESUME_MUST_MATCH = ("data_version", "chunk_rows", "application_schema", "data_root", "feeds")


def resume_fields(cfg: Config, world: generate.World) -> dict[str, object]:
    """Everything that determines a run's rows, normalized through JSON so it compares equal after a jsonb round trip.
    Every World field is recorded, so a future generation input (which must be a World field) is covered too."""
    fields = {
        "data_version": generate.DATA_VERSION,
        "world": {f.name: getattr(world, f.name) for f in dataclasses.fields(world)},
        "chunk_rows": cfg.generator.chunk_rows, "application_schema": cfg.postgres.application_schema,
        "data_root": str(config.data_root(cfg)) if feeds.enabled(cfg) else None,  # nothing written there otherwise
        "feeds": {"enabled": feeds.enabled(cfg), "max_file_size_mb": cfg.filesystem.max_file_size_mb},
    }
    normalized: dict[str, object] = json.loads(json.dumps(fields, default=datetime.isoformat))
    return normalized


def _recorded_world(recorded: dict[str, object]) -> generate.World:
    w = recorded.get("world")
    try:
        assert isinstance(w, dict)
        kwargs: dict[str, Any] = {f.name: datetime.fromisoformat(w[f.name]) if f.type is datetime else w[f.name]
                                  for f in dataclasses.fields(generate.World)}
        return generate.World(**kwargs)
    except (AssertionError, KeyError, TypeError, ValueError, generate.GenerationError) as e:
        raise LoadError(f"run's recorded world is malformed ({w!r}): {e}") from None


def _start_run(conn: psycopg.Connection, cfg: Config, profile: str, world: generate.World) -> str:
    reset_hint = ("To finish a RUNNING or FAILED run use `ecomgen resume`; to start over, drop and recreate the "
                  "database (or its application and metadata schemas) and run init-db")
    with conn.transaction():
        existing = conn.execute(db.q(cfg, "SELECT run_id, state FROM {meta}.generation_runs"
                                        " ORDER BY started_at DESC LIMIT 1")).fetchone()
        if existing:
            raise LoadError(f"database already holds run {existing[0]} ({existing[1]}); IDs are deterministic, "
                            f"so a new run would collide with its rows. {reset_hint}")
        has_rows = sql.SQL(" OR ").join(
            sql.SQL("EXISTS (SELECT 1 FROM {}.{})").format(sql.Identifier(cfg.postgres.application_schema),
                                                           sql.Identifier(t)) for t in TABLES)
        if conn.execute(sql.SQL("SELECT {}").format(has_rows)).fetchone() == (True,):
            raise LoadError(f"source tables already hold rows that no run recorded. {reset_hint}")
        run_id = f"run_{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{secrets.token_hex(3)}"
        planned = {e: world.count(e) for e in generate.ENTITY_ORDER}
        conn.execute(db.q(cfg, "INSERT INTO {meta}.generation_runs"
                             " (run_id, generator_version, seed, config_sha256, profile, state, counts, resume_fields)"
                             " VALUES (%s, %s, %s, %s, %s, 'RUNNING', %s, %s)"),
                     (run_id, __version__, cfg.generator.seed, cfg.sha256, profile, Jsonb({"planned": planned}),
                      Jsonb(resume_fields(cfg, world))))
    return run_id


def write_chunks(conn: psycopg.Connection, cfg: Config, run_id: str, world: generate.World) -> tuple[int, int]:
    """Write every chunk not yet in the ledger for run_id; return (written, skipped)."""
    chunk_rows = cfg.generator.chunk_rows
    done = set(conn.execute(db.q(cfg, "SELECT entity, chunk_no FROM {meta}.generation_chunks WHERE run_id = %s"),
                            (run_id,)).fetchall())
    written = skipped = 0
    for entity in generate.ENTITY_ORDER:
        for chunk_no in range(generate.chunk_count(world, entity, chunk_rows)):
            if (entity, chunk_no) in done:
                skipped += 1
                continue
            chunk = generate.generate_chunk(world, entity, chunk_no, chunk_rows)  # one chunk in memory at a time
            with conn.transaction(), conn.cursor() as cur:
                for table, rows in chunk.tables.items():  # parents before children (orders, items, payments)
                    if rows:
                        _write(cur, cfg, table, rows)
                cur.execute(db.q(cfg, "INSERT INTO {meta}.generation_chunks"
                                    " (run_id, entity, chunk_no, row_count, table_rows) VALUES (%s, %s, %s, %s, %s)"),
                            (run_id, entity, chunk_no, chunk.last - chunk.first + 1,
                             Jsonb({t: len(rows) for t, rows in chunk.tables.items()})))
            written += 1
    return written, skipped


def _complete(conn: psycopg.Connection, cfg: Config, run_id: str) -> dict[str, int]:
    """Mark the run COMPLETED with per-table totals from the ledger, in one statement."""
    row = conn.execute(db.q(cfg, """
        UPDATE {meta}.generation_runs SET state = 'COMPLETED', finished_at = now(),
               counts = counts || jsonb_build_object('written', (
                   SELECT coalesce(jsonb_object_agg(key, n), '{{}}') FROM (
                       SELECT key, sum(value::bigint) AS n
                       FROM {meta}.generation_chunks, jsonb_each_text(table_rows)
                       WHERE run_id = %(run)s GROUP BY key) s))
        WHERE run_id = %(run)s RETURNING counts->'written'"""), {"run": run_id}).fetchone()
    written: dict[str, int] = row[0] if row else {}
    return {t: int(written.get(t, 0)) for t in TABLES}


def run(conn: psycopg.Connection, cfg: Config, preset: str | None = None) -> RunResult:
    """Start a new run and write every chunk. On error the run is marked FAILED (a hard crash leaves it RUNNING)."""
    _require_loadable(conn, cfg)
    _require_data_root(cfg)
    world = world_for(cfg, preset)
    with _run_lock(conn):
        run_id = _start_run(conn, cfg, preset or cfg.generator.profile, world)
        return _execute(conn, cfg, run_id, world)


def resume(conn: psycopg.Connection, cfg: Config, run_id: str | None = None) -> RunResult | None:
    """Finish a RUNNING (crashed) or FAILED run: write only the chunks not yet in its ledger. None if already
    COMPLETED. Holding the run lock proves no other process is writing, so a RUNNING run here is stale."""
    _require_loadable(conn, cfg)
    with _run_lock(conn):
        run_id = db.resolve_run(conn, cfg, run_id)
        row = conn.execute(db.q(cfg, "SELECT state, resume_fields FROM {meta}.generation_runs WHERE run_id = %s"),
                           (run_id,)).fetchone()
        assert row is not None
        state, recorded = row
        if state == "COMPLETED":
            return None
        differs = _resume_mismatch(recorded, cfg)
        if differs:
            raise LoadError(f"can't resume run {run_id}: {', '.join(differs)} differ from what it started with, so "
                            "regenerated chunks wouldn't match committed ones; restore the original config "
                            "(e.g. git checkout) or generator version")
        _require_data_root(cfg)
        world = _recorded_world(recorded)
        # error is kept (a later failure overwrites it), so a crash during resume doesn't lose the first reason
        conn.execute(db.q(cfg, "UPDATE {meta}.generation_runs SET state = 'RUNNING', finished_at = NULL"
                               " WHERE run_id = %s"), (run_id,))
        return _execute(conn, cfg, run_id, world)


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    state: str
    profile: str
    generator_version: str
    resume_mismatch: list[str]  # row-determining inputs that differ from this config/generator; [] = resumable
    started_at: datetime
    finished_at: datetime | None
    error: str | None
    progress: dict[str, tuple[int, int]]  # entity -> (rows in ledger, planned rows)
    writer_pid: int | None  # backend PID holding the run lock (a live writer, or a dead client's lingering session)


def status(conn: psycopg.Connection, cfg: Config, run_id: str | None = None) -> RunStatus:
    """Read-only: never takes the run lock (it inspects pg_locks), so it can't block a real run. The lock is
    checked before and after reading the run, so a writer that starts or finishes meanwhile still counts."""
    holder = _lock_holder(conn)
    run_id = db.resolve_run(conn, cfg, run_id)
    row = conn.execute(db.q(cfg, "SELECT state, profile, generator_version, started_at, finished_at, error,"
                                 " counts->'planned', resume_fields FROM {meta}.generation_runs WHERE run_id = %s"),
                       (run_id,)).fetchone()
    assert row is not None
    state, profile, version, started_at, finished_at, error, planned, recorded = row
    mismatch = _resume_mismatch(recorded, cfg)
    if not isinstance(planned, dict):
        planned = {}
    done: dict[str, int] = dict(conn.execute(db.q(cfg, "SELECT entity, sum(row_count)::int FROM {meta}.generation_chunks"
                                       " WHERE run_id = %s GROUP BY 1"), (run_id,)).fetchall())
    progress = {e: (done.get(e, 0), int(planned.get(e, 0))) for e in generate.ENTITY_ORDER}
    holder = holder or _lock_holder(conn)
    return RunStatus(run_id, state, profile, version, mismatch, started_at, finished_at, error, progress, holder)


def _resume_mismatch(recorded: dict[str, object] | None, cfg: Config) -> list[str]:
    """Names of RESUME_MUST_MATCH fields that differ between the run's record and this config/generator."""
    if recorded is None:
        return ["resume fields (run predates migration 0003)"]
    try:
        current = resume_fields(cfg, generate.World.from_config(cfg))
    except config.ConfigError as e:  # e.g. an invalid ECOMGEN_DATA_ROOT: report it, don't crash status
        return [f"data_root ({e})"]
    return [k for k in RESUME_MUST_MATCH if recorded.get(k) != current[k]]


def _lock_holder(conn: psycopg.Connection) -> int | None:
    # A bigint advisory key shows in pg_locks as classid = high 32 bits, objid = low 32 bits, objsubid = 1.
    row = conn.execute(
        "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted AND objsubid = 1"
        " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
        " AND classid = %s AND objid = %s", (_RUN_LOCK_ID >> 32, _RUN_LOCK_ID & 0xFFFFFFFF)).fetchone()
    return int(row[0]) if row else None


def _require_data_root(cfg: Config) -> None:
    root = Path(config.data_root(cfg))
    if feeds.enabled(cfg) and not root.is_dir():
        raise LoadError(f"data root {root} does not exist; create it or point {config.DATA_ROOT_ENV} at a "
                        "directory (filesystem.csv/jsonl are enabled)")


def _require_loadable(conn: psycopg.Connection, cfg: Config) -> None:
    if not conn.autocommit:
        raise LoadError("loader needs an autocommit connection so each chunk commits on its own")
    if not db.has_ledger(conn, cfg):
        raise LoadError("generator schema not found; run `ecomgen init-db` first")


@contextmanager
def _run_lock(conn: psycopg.Connection) -> Iterator[None]:
    if conn.execute("SELECT pg_try_advisory_lock(%s)", (_RUN_LOCK_ID,)).fetchone() != (True,):
        pid = _lock_holder(conn)
        raise LoadError(f"another generate/resume holds the run lock (backend pid {pid}). If that client is gone "
                        "(e.g. its host lost power), its session lingers until TCP keepalive drops it; end it with "
                        f"SELECT pg_terminate_backend({pid})")
    try:
        yield
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_RUN_LOCK_ID,))
        except psycopg.Error:
            pass  # connection gone: the server released the session lock with it


def _execute(conn: psycopg.Connection, cfg: Config, run_id: str, world: generate.World) -> RunResult:
    try:
        written, skipped = write_chunks(conn, cfg, run_id, world)
        published = feeds.publish(conn, cfg, run_id, world, Path(config.data_root(cfg)))  # DB first, then files
        counts = _complete(conn, cfg, run_id)
    except BaseException as e:
        _fail(conn, cfg, run_id, e)
        raise
    return RunResult(run_id, counts, written, skipped, published)


def _fail(conn: psycopg.Connection, cfg: Config, run_id: str, e: BaseException) -> None:
    try:
        conn.execute(db.q(cfg, "UPDATE {meta}.generation_runs SET state = 'FAILED', finished_at = now(), error = %s"
                             " WHERE run_id = %s AND state = 'RUNNING'"), (f"{type(e).__name__}: {e}"[:2000], run_id))
    except psycopg.Error:
        pass  # connection lost: the run stays RUNNING, which resume handles the same way as a crash
