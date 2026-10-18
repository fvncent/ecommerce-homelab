"""Helper for scripts/homelab_benchmark.sh and scripts/host_snapshot.sh. Not part of the ecomgen package.

Safety rules, enforced here (not in the shell script, not configurable):
- only the three approved databases (DATABASES) are ever connected to for writing; nothing is created or dropped
  here: an operator creates the role and databases by hand, and cleanup is a manual, approved step;
- `preflight` refuses unless the connecting role is unprivileged (no superuser, createdb, createrole, replication,
  bypassrls), owns each approved database, and each is still empty (none of the config's schemas exist), so
  PostgreSQL's own permissions confine the benchmark to those databases;
- URLs must name their database only in the path: `?dbname=` overrides and percent-encoded paths are rejected;
- URLs (they hold passwords) come from env vars, never argv: ECOMGEN_BENCH_URL (any approved database; the others
  are derived from it), ECOMMERCE_DATABASE_URL (crash target), EXISTING_PG_URL (pg-stats, read-only).
"""

import os
import signal
import sys
import time
from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

import psycopg
from psycopg import sql

TEST_DB, SMOKE_DB, CRASH_DB = "ecommerce_homelab_test", "ecommerce_homelab_smoke", "ecommerce_homelab_crash"
DATABASES = (TEST_DB, SMOKE_DB, CRASH_DB)
TABLES = ("sellers", "categories", "customers", "products", "orders", "order_items", "payment_attempts")
PRIVILEGES = ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls")


class Refused(SystemExit):
    def __init__(self, msg: str) -> None:
        super().__init__(f"refused: {msg}")


def env_url(var: str) -> str:
    url = os.environ.get(var, "")
    if not url:
        raise Refused(f"{var} is not set")
    dbname_of(url)  # validates the shape
    return url


def dbname_of(url: str) -> str:
    """The database a URL connects to, refusing forms whose effective database differs from the path."""
    parts = urlsplit(url)
    if "dbname" in parse_qs(parts.query) or "%" in parts.path:
        raise Refused("URL must name its database only in the path (no ?dbname=, no percent-encoding)")
    return parts.path.lstrip("/")


def check_name(name: str, allowed: tuple[str, ...] = DATABASES) -> str:
    if name not in allowed:
        raise Refused(f"database {name!r} is not an approved benchmark database ({', '.join(allowed)})")
    return name


def bench_url() -> str:
    url = env_url("ECOMGEN_BENCH_URL")
    check_name(dbname_of(url))
    return url


def db_url(name: str) -> str:
    return urlsplit(bench_url())._replace(path=f"/{check_name(name)}").geturl()


def _schemas() -> tuple[str, ...]:
    from ecomgen import config
    pg = config.load(os.environ.get("CONFIG", "config/generator.example.yaml")).postgres
    return pg.application_schema, pg.integration_schema, pg.metadata_schema


def preflight() -> None:
    """Refuse unless the role is unprivileged and owns every approved database, each still empty."""
    bench_url()
    schemas = _schemas()
    for name in DATABASES:
        with psycopg.connect(db_url(name)) as c:
            row = c.execute(sql.SQL("SELECT current_user, {}, pg_get_userbyid(d.datdba)"
                                    " FROM pg_roles r, pg_database d"
                                    " WHERE r.rolname = current_user AND d.datname = current_database()").format(
                sql.SQL(", ").join(sql.SQL("r.") + sql.Identifier(p) for p in PRIVILEGES))).fetchone()
            assert row is not None
            user, *flags, owner = row
            held = [p for p, on in zip(PRIVILEGES, flags, strict=True) if on]
            if held:
                raise Refused(f"role {user} holds {', '.join(held)}; the benchmark needs a least-privilege role")
            if owner != user:
                raise Refused(f"database {name} is owned by {owner}, not {user}")
            present = [s for (s,) in c.execute("SELECT nspname FROM pg_namespace WHERE nspname = ANY(%s)",
                                               (list(schemas),))]
            if present:
                raise Refused(f"database {name} is not empty (schemas {', '.join(present)} exist); the benchmark "
                              "never reuses a database: recreate it by hand (approved cleanup) first")
            version = c.execute("SHOW server_version").fetchone()[0]  # type: ignore[index]
            port = c.execute("SELECT inet_server_port()").fetchone()[0]  # type: ignore[index]
    print(f"role {user} (unprivileged) owns {', '.join(DATABASES)}, all empty; PostgreSQL {version}, port {port}")


def server_major() -> None:
    with psycopg.connect(bench_url()) as c:
        print(int(c.execute("SHOW server_version_num").fetchone()[0]) // 10000)  # type: ignore[index]


def wal_lsn() -> None:
    with psycopg.connect(bench_url()) as c:
        print(c.execute("SELECT pg_current_wal_lsn()").fetchone()[0])  # type: ignore[index]


def wal_bytes_since(lsn: str) -> None:
    with psycopg.connect(bench_url()) as c:
        print(int(c.execute("SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), %s)", (lsn,)).fetchone()[0]))  # type: ignore[index]


def sizes(name: str) -> None:
    with psycopg.connect(db_url(name)) as c:
        print(f"database {name} {c.execute('SELECT pg_database_size(current_database())').fetchone()[0]} bytes")  # type: ignore[index]
        for rel, rows, size in c.execute(
            "SELECT relname, n_live_tup, pg_total_relation_size(relid) FROM pg_stat_user_tables"
            " WHERE schemaname = %s ORDER BY 3 DESC", (_schemas()[0],)):
            print(f"  {rel} rows={rows} bytes={size}")


def digest(name: str) -> None:
    """Row count + md5 over every source table, rows sorted by text: equal output = identical data. Row text is
    pinned to UTC/ISO and sorted bytewise (COLLATE "C"), so it doesn't depend on the server's time zone or locale."""
    schema = _schemas()[0]
    with psycopg.connect(db_url(name)) as c:
        c.execute("SET TIME ZONE 'UTC'")
        c.execute("SET DateStyle = 'ISO, MDY'")
        for t in TABLES:
            n, md5 = c.execute(sql.SQL(
                "SELECT count(*), md5(coalesce(string_agg(x::text, '|' ORDER BY x::text COLLATE \"C\"), ''))"
                " FROM {}.{} x").format(sql.Identifier(schema), sql.Identifier(t))).fetchone()  # type: ignore[misc]
            print(f"{t} {n} {md5}")


def leftovers(name: str) -> None:
    """Exit 1 if other sessions or prepared transactions remain in the database (e.g. after a SIGKILL); the killed
    client's backend may take a moment to notice, so wait up to 10 s."""
    with psycopg.connect(db_url(name), autocommit=True) as c:
        for _ in range(50):
            sessions, prepared = c.execute(
                "SELECT (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                " AND pid <> pg_backend_pid()), (SELECT count(*) FROM pg_prepared_xacts"
                " WHERE database = current_database())").fetchone()  # type: ignore[misc]
            if sessions == 0 and prepared == 0:
                print(f"{name}: no other sessions, no prepared transactions")
                return
            time.sleep(0.2)
    print(f"{name}: {sessions} other sessions, {prepared} prepared transactions")
    sys.exit(1)


def pg_stats() -> None:
    """Read-only per-database size and user-table write counters, plus non-default server settings, for every
    PostgreSQL in EXISTING_PG_URL (space-separated), to prove other databases and the server were untouched. User
    tables only: autovacuum/autoanalyze write the system catalogs, so pg_stat_database counters move when idle."""
    urls = os.environ.get("EXISTING_PG_URL", "").split()
    if not urls:
        raise Refused("EXISTING_PG_URL is not set")
    for base in urls:
        dbname_of(base)
    for base in urls:
        parts = urlsplit(base)
        print(f"server {parts.hostname}:{parts.port or 5432}")
        with psycopg.connect(base) as c:
            names = [r[0] for r in c.execute("SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1")]
            roles = [r[0] for r in c.execute("SELECT rolname FROM pg_roles WHERE rolname !~ '^pg_' ORDER BY 1")]
            settings = c.execute("SELECT name, setting FROM pg_settings WHERE source NOT IN ('default', 'override',"
                                 " 'client', 'session') ORDER BY 1").fetchall()
        print(f"roles {' '.join(roles)}")
        for k, v in settings:
            print(f"setting {k}={v}")
        for name in names:
            try:
                with psycopg.connect(parts._replace(path=f"/{name}").geturl(), autocommit=True) as c:
                    c.execute("SET default_transaction_read_only = on")
                    size, ins, upd, dele = c.execute(
                        "SELECT pg_database_size(current_database()), coalesce(sum(n_tup_ins), 0),"
                        " coalesce(sum(n_tup_upd), 0), coalesce(sum(n_tup_del), 0)"
                        " FROM pg_stat_user_tables").fetchone()  # type: ignore[misc]
                print(f"db {name} size={size} user_inserted={ins} user_updated={upd} user_deleted={dele}")
            except psycopg.Error as e:
                print(f"db {name} not readable with this user ({type(e).__name__})")


def crash(config_path: str, entity: str, chunk_no: str) -> None:
    """Run generate against ECOMMERCE_DATABASE_URL (must be the crash database) and SIGKILL this process just
    before writing (entity, chunk_no): a real hard crash."""
    url = env_url("ECOMMERCE_DATABASE_URL")
    check_name(dbname_of(url), (CRASH_DB,))
    from ecomgen import config, generate, load

    real = generate.generate_chunk
    target = (entity, int(chunk_no))

    def killing(world: generate.World, e: str, n: int, rows: int) -> generate.Chunk:
        if (e, n) == target:
            os.kill(os.getpid(), signal.SIGKILL)
        return real(world, e, n, rows)

    generate.generate_chunk = killing  # type: ignore[assignment]
    with psycopg.connect(url, autocommit=True) as conn:
        load.run(conn, config.load(config_path))


COMMANDS: dict[str, Callable[..., None]] = {
    "url": lambda name: print(db_url(name)), "preflight": preflight, "server-major": server_major,
    "wal-lsn": wal_lsn, "wal-bytes-since": wal_bytes_since, "sizes": sizes, "digest": digest,
    "leftovers": leftovers, "pg-stats": pg_stats, "crash": crash,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(f"usage: homelab_bench.py {{{'|'.join(COMMANDS)}}} [args]")
    COMMANDS[sys.argv[1]](*sys.argv[2:])
