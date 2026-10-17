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
    checksum: str


def available_migrations() -> list[Migration]:
    out = []
    for f in sorted(files("ecomgen.migrations").iterdir(), key=lambda f: f.name):
        if f.name.endswith(".sql"):
            text = f.read_text(encoding="utf-8")
            version = int(f.name.split("_", 1)[0])
            out.append(Migration(version, f.name, text, hashlib.sha256(text.encode()).hexdigest()))
    versions = [m.version for m in out]
    if versions != list(range(1, len(out) + 1)):
        raise MigrationError(f"migration versions must be 1..N without gaps, got {versions}")
    return out


def _render(m: Migration, cfg: Config) -> str:
    # Safe: schema names were validated against a lowercase-identifier regex in config.parse.
    pg = cfg.postgres
    return (
        m.sql.replace("{app}", pg.application_schema)
        .replace("{integration}", pg.integration_schema)
        .replace("{meta}", pg.metadata_schema)
    )


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
            if m.version in applied:
                if applied[m.version] != m.checksum:
                    raise MigrationError(f"{m.name} was changed after it was applied; add a new migration instead")
                continue
            conn.execute(_render(m, cfg).encode())  # bytes: run as a multi-statement script
            conn.execute(
                sql.SQL("INSERT INTO {}.schema_migrations (version, name, checksum) VALUES (%s, %s, %s)").format(meta),
                (m.version, m.name, m.checksum),
            )
            done.append(m.name)
    return done
