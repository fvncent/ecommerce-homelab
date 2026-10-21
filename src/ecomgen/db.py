"""PostgreSQL migrations. All pending migrations apply in one transaction: all or nothing."""

import hashlib
from dataclasses import dataclass
from importlib.resources import files

import psycopg
from psycopg import sql

from ecomgen.config import Config

# Arbitrary constant; serializes concurrent init-db runs against the same database.
_MIGRATION_LOCK_ID = 7_302_410_001


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str  # with schema placeholders still in place


def available_migrations() -> list[Migration]:
    out = []
    for f in sorted(files("ecomgen.migrations").iterdir(), key=lambda f: f.name):
        if f.name.endswith(".sql"):
            prefix = f.name.split("_", 1)[0]
            if not prefix.isdigit():
                raise MigrationError(f"migration file must start with NNNN_: {f.name}")
            out.append(Migration(int(prefix), f.name, f.read_text(encoding="utf-8")))
    versions = [m.version for m in out]
    if versions != list(range(1, len(out) + 1)):
        raise MigrationError(f"migration versions must be 1..N without gaps, got {versions}")
    return out


def _render(m: Migration, cfg: Config, conn: psycopg.Connection) -> str:
    pg = cfg.postgres
    text = m.sql
    for placeholder, name in (
        ("{app}", pg.application_schema),
        ("{integration}", pg.integration_schema),
        ("{meta}", pg.metadata_schema),
    ):
        text = text.replace(placeholder, sql.Identifier(name).as_string(conn))
    return text


OUTBOX_MIGRATION = 9  # creates {integration}.outbox_events, which belongs to exactly one schema set


def pg_name(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def migrate(conn: psycopg.Connection, cfg: Config) -> list[str]:
    """Apply pending migrations; return the names applied. Raises MigrationError on checksum drift."""
    meta = sql.Identifier(cfg.postgres.metadata_schema)
    migrations = available_migrations()
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_ID,))
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(meta))
        conn.execute(sql.SQL(
            "CREATE TABLE IF NOT EXISTS {}.schema_migrations ("
            " version integer PRIMARY KEY, name text NOT NULL, checksum char(64) NOT NULL,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        ).format(meta))
        applied: dict[int, str] = dict(conn.execute(
            sql.SQL("SELECT version, checksum FROM {}.schema_migrations").format(meta)
        ).fetchall())
        unknown = set(applied) - {m.version for m in migrations}
        if unknown:
            raise MigrationError(f"database has migrations this generator doesn't know: {sorted(unknown)}")
        done = []
        for m in migrations:
            rendered = _render(m, cfg, conn)
            # Checksum of the rendered SQL, so a schema rename in config is caught, not reported "up to date".
            checksum = hashlib.sha256(rendered.encode()).hexdigest()
            if m.version in applied:
                if applied[m.version] != checksum:
                    raise MigrationError(
                        f"{m.name} differs from what was applied: the file was edited (add a new migration "
                        "instead) or schema names in config changed"
                    )
                continue
            if m.version == OUTBOX_MIGRATION and has_outbox(conn, cfg):
                raise MigrationError(
                    f"integration schema {pg_name(cfg.postgres.integration_schema)} already holds outbox_events of "
                    "another schema set; give this config its own integration_schema (each dataset needs new "
                    "application, integration and metadata schema names)")
            conn.execute(rendered.encode())  # bytes: run as a multi-statement script
            conn.execute(
                sql.SQL("INSERT INTO {}.schema_migrations (version, name, checksum) VALUES (%s, %s, %s)").format(meta),
                (m.version, m.name, checksum),
            )
            done.append(m.name)
    return done


def q(cfg: Config, query: str) -> sql.Composed:
    """Format {app}/{integration}/{meta} in query with the configured, quoted schema names."""
    return sql.SQL(query).format(app=sql.Identifier(cfg.postgres.application_schema),
                                 integration=sql.Identifier(cfg.postgres.integration_schema),
                                 meta=sql.Identifier(cfg.postgres.metadata_schema))


def _meta_regclass(conn: psycopg.Connection, cfg: Config, table: str, schema: str | None = None) -> str:
    """Quoted `<schema>.<table>` for to_regclass (default: the metadata schema; both quoted, so any configured schema
    name works)."""
    return sql.SQL("{}.{}").format(sql.Identifier(schema or cfg.postgres.metadata_schema),
                                   sql.Identifier(table)).as_string(conn)


def has_table(conn: psycopg.Connection, cfg: Config, table: str, schema: str | None = None) -> bool:
    """True if the metadata schema (or `schema`) has this table."""
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL",
                        (_meta_regclass(conn, cfg, table, schema),)).fetchone() == (True,)


def has_column(conn: psycopg.Connection, cfg: Config, table: str, column: str) -> bool:
    """True if the metadata schema's table has this column (e.g. one added by a later migration)."""
    return conn.execute("SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass(%s)"
                        " AND attname = %s AND NOT attisdropped)",
                        (_meta_regclass(conn, cfg, table), column)).fetchone() == (True,)


def has_outbox(conn: psycopg.Connection, cfg: Config) -> bool:
    """True once migration 0009 has created the integration schema's outbox_events."""
    return has_table(conn, cfg, "outbox_events", cfg.postgres.integration_schema)


def has_migration(conn: psycopg.Connection, cfg: Config, version: int) -> bool:
    """True if this schema set (its metadata schema) has applied migration `version`."""
    return has_table(conn, cfg, "schema_migrations") and conn.execute(
        q(cfg, "SELECT EXISTS (SELECT 1 FROM {meta}.schema_migrations WHERE version = %s)"),
        (version,)).fetchone() == (True,)


def clock_columns(conn: psycopg.Connection, cfg: Config) -> str:
    """SQL select list `previous_as_of, as_of` of generation_runs; typed NULLs before migration 0008 (an untyped NULL
    in a subquery becomes text, which can't be compared or coalesced with a timestamp)."""
    if has_column(conn, cfg, "generation_runs", "as_of"):
        return "previous_as_of, as_of"
    return "NULL::timestamptz, NULL::timestamptz"


def has_ledger(conn: psycopg.Connection, cfg: Config) -> bool:
    """True once init-db has created the run ledger."""
    return has_table(conn, cfg, "generation_runs")


class RunLookupError(RuntimeError):
    pass


def resolve_run(conn: psycopg.Connection, cfg: Config, run_id: str | None) -> str:
    """The given run if it exists, else the most recent run. Raises RunLookupError."""
    if not has_ledger(conn, cfg):
        raise RunLookupError("generator schema not found; run `ecomgen init-db` first")
    if run_id is None:
        row = conn.execute(q(cfg, "SELECT run_id FROM {meta}.generation_runs ORDER BY started_at DESC LIMIT 1")).fetchone()
        if row is None:
            raise RunLookupError("no runs recorded; run `ecomgen generate` first")
    else:
        row = conn.execute(q(cfg, "SELECT run_id FROM {meta}.generation_runs WHERE run_id = %s"), (run_id,)).fetchone()
        if row is None:
            raise RunLookupError(f"run {run_id} not found")
    return str(row[0])
