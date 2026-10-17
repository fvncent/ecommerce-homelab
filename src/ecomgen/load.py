"""Write generated chunks to PostgreSQL with a run/chunk ledger.

Each chunk's rows and its generation_chunks marker commit in one transaction: a marker means the chunk is
durable; no marker means nothing from it was committed. Chunks already marked for a run are skipped, which
is what makes a re-run of the same run (resume) idempotent.
"""

import secrets
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from ecomgen import __version__, db, generate
from ecomgen.config import Config

# Built-in profile counts; other profiles take counts from the config file.
PROFILE_COUNTS = {"smoke": {"sellers": 20, "customers": 500, "products": 200, "orders": 1000}}
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


def world_for(cfg: Config, preset: str | None) -> generate.World:
    """Config counts, or a built-in preset's counts when one is named explicitly (e.g. --profile smoke)."""
    world = generate.World.from_config(cfg)
    if preset is None:
        return world
    c = PROFILE_COUNTS[preset]
    return replace(world, sellers=c["sellers"], customers=c["customers"], products=c["products"], orders=c["orders"])


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


def _start_run(conn: psycopg.Connection, cfg: Config, profile: str, world: generate.World) -> str:
    reset_hint = ("`resume` is not implemented yet; to start over, drop and recreate the database (or its "
                  "application and metadata schemas) and run init-db")
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
                             " (run_id, generator_version, seed, config_sha256, profile, state, counts)"
                             " VALUES (%s, %s, %s, %s, %s, 'RUNNING', %s)"),
                     (run_id, __version__, cfg.generator.seed, cfg.sha256, profile, Jsonb({"planned": planned})))
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
    if not conn.autocommit:
        raise LoadError("loader needs an autocommit connection so each chunk commits on its own")
    world = world_for(cfg, preset)
    if not db.has_ledger(conn, cfg):
        raise LoadError("generator schema not found; run `ecomgen init-db` first")
    if conn.execute("SELECT pg_try_advisory_lock(%s)", (_RUN_LOCK_ID,)).fetchone() != (True,):
        raise LoadError("another generate/resume is running against this database")
    try:
        run_id = _start_run(conn, cfg, preset or cfg.generator.profile, world)
        try:
            written, skipped = write_chunks(conn, cfg, run_id, world)
            counts = _complete(conn, cfg, run_id)
        except BaseException as e:
            _fail(conn, cfg, run_id, e)
            raise
        return RunResult(run_id, counts, written, skipped)
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_RUN_LOCK_ID,))
        except psycopg.Error:
            pass  # connection gone: the server released the session lock with it


def _fail(conn: psycopg.Connection, cfg: Config, run_id: str, e: BaseException) -> None:
    try:
        conn.execute(db.q(cfg, "UPDATE {meta}.generation_runs SET state = 'FAILED', finished_at = now(), error = %s"
                             " WHERE run_id = %s AND state = 'RUNNING'"), (f"{type(e).__name__}: {e}"[:2000], run_id))
    except psycopg.Error:
        pass  # connection lost: the run stays RUNNING, which resume handles the same way as a crash
