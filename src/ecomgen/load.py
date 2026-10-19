"""Write generated chunks to PostgreSQL with a run/chunk ledger.

Each chunk's rows and its generation_chunks marker commit in one transaction: a marker means the chunk is
durable; no marker means nothing from it was committed. Chunks already marked for a run are skipped, which
is what makes a re-run of the same run (resume) idempotent.

Two kinds of run: `generate` writes a whole dataset as of clock_end in one run; `advance` writes clock steps, each
a run that inserts the orders placed since the previous step and updates earlier ones that changed (an update
applies only to the row exactly as the previous step left it). A schema set holds one kind only.
"""

import dataclasses
import json
import secrets
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from ecomgen import __version__, config, db, feeds, generate
from ecomgen.config import Config

# Built-in profile counts; other profiles take counts from the config file.
PROFILE_COUNTS = {"smoke": {"sellers": 20, "customers": 500, "products": 200, "orders": 1000,
                             "clickstream_events": 10_000}}
TABLES = ("sellers", "categories", "customers", "products", "orders", "order_items", "payment_attempts")
OUTBOX = "outbox_events"  # in the integration schema; written in the same transaction as the rows it describes
# Tables a clock step updates: (key, changing column); updated_at changes with it. Ledger key: "<table>_updated".
UPDATED = {"orders": ("order_id", "status"), "payment_attempts": ("payment_id", "state")}
# Arbitrary constant; held for the whole run so two generate/resume processes never write concurrently.
_RUN_LOCK_ID = 7_302_410_002


class LoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunResult:
    run_id: str
    counts: dict[str, int]  # rows written (inserted) per table, from the chunk ledger
    chunks_written: int
    chunks_skipped: int
    files: feeds.Published
    updated: dict[str, int] = dataclasses.field(default_factory=dict)  # clock steps: rows updated per table
    previous_as_of: datetime | None = None  # clock steps only
    as_of: datetime | None = None


def world_for(cfg: Config, preset: str | None) -> generate.World:
    """Config counts, or a built-in preset's counts when one is named explicitly (e.g. --profile smoke)."""
    world = generate.World.from_config(cfg)
    if preset is None:
        return world
    c = PROFILE_COUNTS[preset]
    return replace(world, sellers=c["sellers"], customers=c["customers"], products=c["products"], orders=c["orders"],
                   clickstream_events=c["clickstream_events"])


def _table(cfg: Config, table: str) -> sql.Identifier:
    schema = cfg.postgres.integration_schema if table == OUTBOX else cfg.postgres.application_schema
    return sql.Identifier(schema, table)


def _stamped(table: str, rows: Sequence[tuple[object, ...]], run_id: str) -> Sequence[tuple[object, ...]]:
    """Outbox events carry the run that wrote them (lineage); generation leaves run_id empty."""
    if table != OUTBOX:
        return rows
    return [cast(generate.OutboxEvent, r)._replace(run_id=run_id) for r in rows]


def _write(cur: psycopg.Cursor[tuple[object, ...]], cfg: Config, table: str,
           rows: Sequence[tuple[object, ...]]) -> None:
    fields: tuple[str, ...] = getattr(rows[0], "_fields")  # by column name, never positional
    target = sql.SQL("{} ({})").format(_table(cfg, table), sql.SQL(", ").join(map(sql.Identifier, fields)))
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
# Every step of a chain must generate the same world into the same schema and publish the same feeds under the same
# data root (validate checks the chain's files together); chunk_rows and max_file_size_mb (how files are split) may
# differ per step.
CHAIN_MUST_MATCH = ("data_version", "world", "application_schema", "data_root", "feeds.enabled")


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


def recorded_world(recorded: dict[str, object]) -> generate.World:
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
                  "database (or all three of its application, integration and metadata schemas) and run init-db")
    with conn.transaction():
        existing = conn.execute(db.q(cfg, "SELECT run_id, state FROM {meta}.generation_runs"
                                        " ORDER BY started_at DESC LIMIT 1")).fetchone()
        if existing:
            raise LoadError(f"database already holds run {existing[0]} ({existing[1]}); IDs are deterministic, "
                            f"so a new run would collide with its rows. {reset_hint}")
        if _has_rows(conn, cfg):
            raise LoadError(f"source tables already hold rows that no run recorded. {reset_hint}")
        return _insert_run(conn, cfg, profile, world, {e: world.count(e) for e in generate.ENTITY_ORDER})


def _has_rows(conn: psycopg.Connection, cfg: Config) -> bool:
    has_rows = sql.SQL(" OR ").join(
        sql.SQL("EXISTS (SELECT 1 FROM {})").format(_table(cfg, t)) for t in (*TABLES, OUTBOX))
    return conn.execute(sql.SQL("SELECT {}").format(has_rows)).fetchone() == (True,)


def _insert_run(conn: psycopg.Connection, cfg: Config, profile: str, world: generate.World, planned: dict[str, int],
                step: tuple[datetime | None, datetime] | None = None) -> str:
    run_id = f"run_{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{secrets.token_hex(3)}"
    columns, values = "", ""
    params: list[object] = [run_id, __version__, cfg.generator.seed, cfg.sha256, profile, Jsonb({"planned": planned}),
                            Jsonb(resume_fields(cfg, world))]
    if step is not None:
        columns, values = ", previous_as_of, as_of", ", %s, %s"
        params += list(step)
    conn.execute(db.q(cfg, "INSERT INTO {meta}.generation_runs (run_id, generator_version, seed, config_sha256,"
                           f" profile, state, counts, resume_fields{columns})"
                           f" VALUES (%s, %s, %s, %s, %s, 'RUNNING', %s, %s{values})"), params)
    return run_id


def _done(conn: psycopg.Connection, cfg: Config, run_id: str) -> set[tuple[str, int]]:
    return set(conn.execute(db.q(cfg, "SELECT entity, chunk_no FROM {meta}.generation_chunks WHERE run_id = %s"),
                            (run_id,)).fetchall())


def write_chunks(conn: psycopg.Connection, cfg: Config, run_id: str, world: generate.World,
                 entities: Sequence[str] = generate.ENTITY_ORDER) -> tuple[int, int]:
    """Write every chunk of these entities not yet in the ledger for run_id; return (written, skipped)."""
    chunk_rows = cfg.generator.chunk_rows
    done = _done(conn, cfg, run_id)
    written = skipped = 0
    for entity in entities:
        for chunk_no in range(generate.chunk_count(world, entity, chunk_rows)):
            if (entity, chunk_no) in done:
                skipped += 1
                continue
            chunk = generate.generate_chunk(world, entity, chunk_no, chunk_rows)  # one chunk in memory at a time
            with conn.transaction(), conn.cursor() as cur:
                for table, rows in chunk.tables.items():  # parents before children (orders, items, payments, events)
                    if rows:
                        _write(cur, cfg, table, _stamped(table, rows, run_id))
                cur.execute(db.q(cfg, "INSERT INTO {meta}.generation_chunks"
                                    " (run_id, entity, chunk_no, row_count, table_rows) VALUES (%s, %s, %s, %s, %s)"),
                            (run_id, entity, chunk_no, chunk.last - chunk.first + 1,
                             Jsonb({t: len(rows) for t, rows in chunk.tables.items()})))
            written += 1
    return written, skipped


def _update(cur: psycopg.Cursor[tuple[object, ...]], cfg: Config, table: str,
            pairs: Sequence[tuple[tuple[object, ...], tuple[object, ...]]]) -> None:
    """Apply (before, after) changes; each must find its row exactly as `before` left it, or nothing is written."""
    if not pairs:
        return
    key, col = UPDATED[table]
    cur.execute(sql.SQL(
        "UPDATE {t} x SET {col} = u.new, updated_at = u.new_at"
        " FROM unnest(%s::text[], %s::text[], %s::timestamptz[], %s::text[], %s::timestamptz[])"
        " AS u (id, old, old_at, new, new_at)"
        " WHERE x.{key} = u.id AND x.{col} = u.old AND x.updated_at = u.old_at").format(
        t=sql.Identifier(cfg.postgres.application_schema, table), col=sql.Identifier(col), key=sql.Identifier(key)),
        [[getattr(a, key) for a, _ in pairs], [getattr(a, col) for a, _ in pairs],
         [getattr(a, "updated_at") for a, _ in pairs], [getattr(b, col) for _, b in pairs],
         [getattr(b, "updated_at") for _, b in pairs]])
    if cur.rowcount != len(pairs):
        raise LoadError(f"{table}: {len(pairs) - cur.rowcount} of {len(pairs)} row(s) are not as the previous step "
                        "left them (changed outside the generator?); nothing from this chunk was written")


def write_step(conn: psycopg.Connection, cfg: Config, run_id: str, world: generate.World,
               previous: datetime | None, as_of: datetime) -> tuple[int, int]:
    """Write a clock step's chunks not yet in the ledger: master data in the first step, then the orders placed or
    changed in [previous, as_of). Return (written, skipped)."""
    written, skipped = (0, 0) if previous is not None else write_chunks(conn, cfg, run_id, world,
                                                                        generate.MASTER_ENTITIES)
    chunk_rows = cfg.generator.chunk_rows
    plan = generate.plan_step(world, previous, as_of)
    done = _done(conn, cfg, run_id)
    for chunk_no in range(generate.step_chunk_count(plan, chunk_rows)):
        if ("orders", chunk_no) in done:
            skipped += 1
            continue
        chunk = generate.step_chunk(world, plan, chunk_no, chunk_rows)
        with conn.transaction(), conn.cursor() as cur:
            for table, rows in chunk.inserts.items():  # orders before their items and payments
                if rows:
                    _write(cur, cfg, table, rows)
            _update(cur, cfg, "orders", chunk.order_updates)
            _update(cur, cfg, "payment_attempts", chunk.payment_updates)
            if chunk.events:  # after the rows they reference; same transaction, so never a change without its event
                _write(cur, cfg, OUTBOX, _stamped(OUTBOX, chunk.events, run_id))
            table_rows = {t: len(rows) for t, rows in chunk.inserts.items()} | {
                "orders_updated": len(chunk.order_updates), "payment_attempts_updated": len(chunk.payment_updates),
                OUTBOX: len(chunk.events)}
            cur.execute(db.q(cfg, "INSERT INTO {meta}.generation_chunks"
                                " (run_id, entity, chunk_no, row_count, table_rows) VALUES (%s, %s, %s, %s, %s)"),
                        (run_id, "orders", chunk_no, chunk.last - chunk.first + 1, Jsonb(table_rows)))
        written += 1
    return written, skipped


def _complete(conn: psycopg.Connection, cfg: Config, run_id: str) -> dict[str, int]:
    """Mark the run COMPLETED with per-key totals from the ledger (inserted rows per table, and `<table>_updated`
    for clock steps), in one statement."""
    row = conn.execute(db.q(cfg, """
        UPDATE {meta}.generation_runs SET state = 'COMPLETED', finished_at = now(),
               counts = counts || jsonb_build_object('written', (
                   SELECT coalesce(jsonb_object_agg(key, n), '{{}}') FROM (
                       SELECT key, sum(value::bigint) AS n
                       FROM {meta}.generation_chunks, jsonb_each_text(table_rows)
                       WHERE run_id = %(run)s GROUP BY key) s))
        WHERE run_id = %(run)s RETURNING counts->'written'"""), {"run": run_id}).fetchone()
    written: dict[str, int] = row[0] if row else {}
    return {k: int(v) for k, v in written.items()}


def run(conn: psycopg.Connection, cfg: Config, preset: str | None = None) -> RunResult:
    """Start a new run and write every chunk. On error the run is marked FAILED (a hard crash leaves it RUNNING)."""
    _require_loadable(conn, cfg)
    _require_data_root(cfg)
    world = world_for(cfg, preset)
    with _run_lock(conn):
        run_id = _start_run(conn, cfg, preset or cfg.generator.profile, world)
        return _execute(conn, cfg, run_id, world)


def advance(conn: psycopg.Connection, cfg: Config, to: datetime | None = None) -> RunResult | None:
    """Move the simulation clock to `to` (default clock_end) as a new step run: database changes first, then the
    step's files (its window's events), then COMPLETED. None if a completed step already reached `to`. The first
    step starts from an empty schema set; each later one needs the previous step COMPLETED and the same world, data
    root and feeds."""
    _require_loadable(conn, cfg)
    _require_clock(conn, cfg)
    _require_data_root(cfg)
    world = generate.World.from_config(cfg)
    if to is not None and to.utcoffset() is None:
        raise LoadError(f"as_of {to.isoformat()} has no time zone")
    as_of = world.clock_end if to is None else to.astimezone(UTC)
    generate.check_step(world, None, as_of)  # in the window; before any comparison with the chain
    with _run_lock(conn):
        started = _start_step(conn, cfg, world, as_of)
        if started is None:
            return None
        run_id, previous = started
        return _execute(conn, cfg, run_id, world, (previous, as_of))


def _start_step(conn: psycopg.Connection, cfg: Config, world: generate.World,
                as_of: datetime) -> tuple[str, datetime | None] | None:
    """Under the run lock: check the chain and record the new step. None if a COMPLETED step already reached as_of
    (the latest or an earlier one: repeating a list of steps is a no-op)."""
    with conn.transaction():
        one_shot = conn.execute(db.q(cfg, "SELECT run_id FROM {meta}.generation_runs WHERE as_of IS NULL"
                                          " ORDER BY started_at LIMIT 1")).fetchone()
        if one_shot:
            raise LoadError(f"this schema set holds generate run {one_shot[0]}; clock steps need their own schema set "
                            "(new application/integration/metadata schema names in the config, then init-db)")
        last = conn.execute(db.q(cfg, "SELECT run_id, state, as_of, resume_fields FROM {meta}.generation_runs"
                                      " ORDER BY as_of DESC LIMIT 1")).fetchone()
        previous = None
        if last is None:
            if _has_rows(conn, cfg):
                raise LoadError("source tables already hold rows that no run recorded; clock steps need an empty "
                                "schema set")
        else:
            last_id, state, last_as_of, recorded = last
            previous = last_as_of.astimezone(UTC)  # psycopg returns the session's time zone
            if state != "COMPLETED":
                raise LoadError(f"step {last_id} is {state}: finish it with `ecomgen resume` first")
            differs = _mismatch(recorded, resume_fields(cfg, world), CHAIN_MUST_MATCH)
            if differs:
                raise LoadError(f"{', '.join(differs)} differ from the chain's (step {last_id}): every step must use "
                                "the chain's config; for another dataset use new schema names")
            if as_of <= previous:
                reached = conn.execute(db.q(cfg, "SELECT 1 FROM {meta}.generation_runs WHERE as_of = %s"),
                                       (as_of,)).fetchone()
                if reached:
                    return None
                raise LoadError(f"the clock is at {previous.isoformat()} (step {last_id}); it only moves forward, "
                                f"and no step stopped at {as_of.isoformat()}")
        planned = {} if previous is not None else {e: world.count(e) for e in generate.MASTER_ENTITIES}
        orders = len(generate.plan_step(world, previous, as_of).orders)
        if orders:
            planned["orders"] = orders
        return _insert_run(conn, cfg, cfg.generator.profile, world, planned, (previous, as_of)), previous


def resume(conn: psycopg.Connection, cfg: Config, run_id: str | None = None) -> RunResult | None:
    """Finish a RUNNING (crashed) or FAILED run: write only the chunks not yet in its ledger. None if already
    COMPLETED. Holding the run lock proves no other process is writing, so a RUNNING run here is stale."""
    _require_loadable(conn, cfg)
    with _run_lock(conn):
        run_id = db.resolve_run(conn, cfg, run_id)
        row = conn.execute(db.q(cfg, f"SELECT state, resume_fields, {_clock_columns(conn, cfg)}"
                                     " FROM {meta}.generation_runs WHERE run_id = %s"), (run_id,)).fetchone()
        assert row is not None
        state, recorded, previous_as_of, as_of = row
        previous_as_of, as_of = _utc(previous_as_of), _utc(as_of)  # psycopg returns the session's time zone
        if state == "COMPLETED":
            return None
        differs = _resume_mismatch(recorded, cfg)
        if differs:
            raise LoadError(f"can't resume run {run_id}: {', '.join(differs)} differ from what it started with, so "
                            "regenerated chunks wouldn't match committed ones; restore the original config "
                            "(e.g. git checkout) or generator version")
        _require_data_root(cfg)
        world = recorded_world(recorded)
        # error is kept (a later failure overwrites it), so a crash during resume doesn't lose the first reason
        conn.execute(db.q(cfg, "UPDATE {meta}.generation_runs SET state = 'RUNNING', finished_at = NULL"
                               " WHERE run_id = %s"), (run_id,))
        return _execute(conn, cfg, run_id, world, None if as_of is None else (previous_as_of, as_of))


def _utc(t: datetime | None) -> datetime | None:
    return None if t is None else t.astimezone(UTC)


def _clock_columns(conn: psycopg.Connection, cfg: Config) -> str:
    """previous_as_of, as_of (NULL, NULL before migration 0008)."""
    return "previous_as_of, as_of" if db.has_column(conn, cfg, "generation_runs", "as_of") else "NULL, NULL"


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
    previous_as_of: datetime | None = None  # clock steps: the clock moved from previous_as_of (None: start) ...
    as_of: datetime | None = None  # ... to as_of; None for a generate run


def status(conn: psycopg.Connection, cfg: Config, run_id: str | None = None) -> RunStatus:
    """Read-only: never takes the run lock (it inspects pg_locks), so it can't block a real run. The lock is
    checked before and after reading the run, so a writer that starts or finishes meanwhile still counts."""
    holder = _lock_holder(conn)
    run_id = db.resolve_run(conn, cfg, run_id)
    row = conn.execute(db.q(cfg, "SELECT state, profile, generator_version, started_at, finished_at, error,"
                                 f" counts->'planned', resume_fields, {_clock_columns(conn, cfg)}"
                                 " FROM {meta}.generation_runs WHERE run_id = %s"), (run_id,)).fetchone()
    assert row is not None
    state, profile, version, started_at, finished_at, error, planned, recorded, previous_as_of, as_of = row
    mismatch = _resume_mismatch(recorded, cfg)
    if not isinstance(planned, dict):
        planned = {}
    done: dict[str, int] = dict(conn.execute(db.q(cfg, "SELECT entity, sum(row_count)::int FROM {meta}.generation_chunks"
                                       " WHERE run_id = %s GROUP BY 1"), (run_id,)).fetchall())
    progress = {e: (done.get(e, 0), int(planned.get(e, 0))) for e in generate.ENTITY_ORDER
                if as_of is None or e in planned or e in done}  # a step plans only what it writes
    holder = holder or _lock_holder(conn)
    return RunStatus(run_id, state, profile, version, mismatch, started_at, finished_at, error, progress, holder,
                     _utc(previous_as_of), _utc(as_of))


def _resume_mismatch(recorded: dict[str, object] | None, cfg: Config) -> list[str]:
    """Names of RESUME_MUST_MATCH fields that differ between the run's record and this config/generator."""
    if recorded is None:
        return ["resume fields (run predates migration 0003)"]
    try:
        current = resume_fields(cfg, generate.World.from_config(cfg))
    except config.ConfigError as e:  # e.g. an invalid ECOMGEN_DATA_ROOT: report it, don't crash status
        return [f"data_root ({e})"]
    return _mismatch(recorded, current, RESUME_MUST_MATCH)


def _mismatch(recorded: dict[str, Any], current: dict[str, Any], keys: Sequence[str]) -> list[str]:
    """Keys whose values differ; "a.b" compares a nested field."""
    def get(d: dict[str, Any], key: str) -> object:
        for part in key.split("."):
            d = d.get(part) if isinstance(d, dict) else None  # type: ignore[assignment]
        return d
    return [k for k in keys if get(recorded, k) != get(current, k)]


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


def _require_clock(conn: psycopg.Connection, cfg: Config) -> None:
    if not db.has_column(conn, cfg, "generation_runs", "as_of"):
        raise LoadError("this schema predates clock steps (migration 0008); run `ecomgen init-db` first")


def _require_loadable(conn: psycopg.Connection, cfg: Config) -> None:
    if not conn.autocommit:
        raise LoadError("loader needs an autocommit connection so each chunk commits on its own")
    if not db.has_ledger(conn, cfg):
        raise LoadError("generator schema not found; run `ecomgen init-db` first")
    if not db.has_outbox(conn, cfg):
        raise LoadError("this schema predates the outbox (migration 0009); run `ecomgen init-db` first")


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


def _execute(conn: psycopg.Connection, cfg: Config, run_id: str, world: generate.World,
             step: tuple[datetime | None, datetime] | None = None) -> RunResult:
    """Write a generate run (step None) or a clock step (previous_as_of, as_of), then mark it COMPLETED."""
    try:
        if step is None:
            written, skipped = write_chunks(conn, cfg, run_id, world)
            published = feeds.publish(conn, cfg, run_id, world, Path(config.data_root(cfg)))  # DB first, then files
        else:
            written, skipped = write_step(conn, cfg, run_id, world, *step)
            published = feeds.publish(conn, cfg, run_id, world, Path(config.data_root(cfg)),
                                      generate.plan_step(world, *step))  # the step's window only
        totals = _complete(conn, cfg, run_id)
    except BaseException as e:
        _fail(conn, cfg, run_id, e)
        raise
    counts = {t: totals.get(t, 0) for t in (*TABLES, OUTBOX)}
    if step is None:
        return RunResult(run_id, counts, written, skipped, published)
    updated = {t: totals.get(f"{t}_updated", 0) for t in UPDATED}
    return RunResult(run_id, counts, written, skipped, published, updated, *step)


def _fail(conn: psycopg.Connection, cfg: Config, run_id: str, e: BaseException) -> None:
    try:
        conn.execute(db.q(cfg, "UPDATE {meta}.generation_runs SET state = 'FAILED', finished_at = now(), error = %s"
                             " WHERE run_id = %s AND state = 'RUNNING'"), (f"{type(e).__name__}: {e}"[:2000], run_id))
    except psycopg.Error:
        pass  # connection lost: the run stays RUNNING, which resume handles the same way as a crash
