"""Helper for scripts/homelab_benchmark.sh and scripts/host_snapshot.sh. Not part of the ecomgen package.

Safety rules, enforced here (not in the shell script, not configurable):
- databases are only created inside NAMESPACE (`ecommerce_homelab_test` or `ecommerce_homelab_test_*`) and only
  dropped if they are siblings (`ecommerce_homelab_test_*`); the kept benchmark DB itself is never dropped;
- `create` refuses a database that already exists;
- URLs must name their database only in the path: `?dbname=` overrides and percent-encoded paths are rejected;
- URLs (they hold passwords) come from env vars, never argv: ECOMGEN_TEST_DATABASE_URL (admin, our container's
  maintenance DB), ECOMMERCE_DATABASE_URL (crash target), EXISTING_PG_URL (pg-stats, read-only).
"""

import os
import re
import signal
import sys
from collections.abc import Callable
from urllib.parse import parse_qs, urlsplit

import psycopg
from psycopg import sql

NAMESPACE = "ecommerce_homelab_test"
NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")
TABLES = ("sellers", "categories", "customers", "products", "orders", "order_items", "payment_attempts")


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


def admin_url() -> str:
    return env_url("ECOMGEN_TEST_DATABASE_URL")


def check_name(name: str, *, sibling: bool = False) -> str:
    ok = NAME.fullmatch(name) and (name.startswith(NAMESPACE + "_") if sibling
                                   else name == NAMESPACE or name.startswith(NAMESPACE + "_"))
    if not ok:
        kind = f"{NAMESPACE}_*" if sibling else f"{NAMESPACE} or {NAMESPACE}_*"
        raise Refused(f"database {name!r} is outside the benchmark namespace ({kind})")
    return name


def db_url(name: str) -> str:
    return urlsplit(admin_url())._replace(path=f"/{check_name(name)}").geturl()


def exists(name: str) -> bool:
    with psycopg.connect(admin_url()) as c:
        return c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is not None


def create(name: str) -> None:
    check_name(name)
    if exists(name):
        raise Refused(f"database {name} already exists; the benchmark never reuses a database")
    with psycopg.connect(admin_url(), autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    print(f"created {name}")


def drop(name: str) -> None:
    check_name(name, sibling=True)
    with psycopg.connect(admin_url(), autocommit=True) as c:
        c.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
    print(f"dropped {name}")


def system_id() -> None:
    """Cluster identity, compared with the container's own value so we know the URL reaches our container."""
    with psycopg.connect(admin_url()) as c:
        print(c.execute("SELECT system_identifier FROM pg_control_system()").fetchone()[0])  # type: ignore[index]


def wal_lsn() -> None:
    with psycopg.connect(admin_url()) as c:
        print(c.execute("SELECT pg_current_wal_lsn()").fetchone()[0])  # type: ignore[index]


def wal_bytes_since(lsn: str) -> None:
    with psycopg.connect(admin_url()) as c:
        print(int(c.execute("SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), %s)", (lsn,)).fetchone()[0]))  # type: ignore[index]


def _schema() -> str:
    from ecomgen import config
    return config.load(os.environ.get("CONFIG", "config/generator.example.yaml")).postgres.application_schema


def sizes(name: str) -> None:
    with psycopg.connect(db_url(name)) as c:
        print(f"database {name} {c.execute('SELECT pg_database_size(current_database())').fetchone()[0]} bytes")  # type: ignore[index]
        for rel, rows, size in c.execute(
            "SELECT relname, n_live_tup, pg_total_relation_size(relid) FROM pg_stat_user_tables"
            " WHERE schemaname = %s ORDER BY 3 DESC", (_schema(),)):
            print(f"  {rel} rows={rows} bytes={size}")


def digest(name: str) -> None:
    """Row count + md5 over every source table, rows sorted by text: equal output = identical data."""
    schema = _schema()
    with psycopg.connect(db_url(name)) as c:
        for t in TABLES:
            n, md5 = c.execute(sql.SQL(
                "SELECT count(*), md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) FROM {}.{} x"
            ).format(sql.Identifier(schema), sql.Identifier(t))).fetchone()  # type: ignore[misc]
            print(f"{t} {n} {md5}")


def pg_stats() -> None:
    """Read-only per-database size and user-table write counters (EXISTING_PG_URL), to prove other databases were
    untouched. User tables only: autovacuum/autoanalyze write the system catalogs, so pg_stat_database's tuple
    counters can move on an idle database."""
    base = env_url("EXISTING_PG_URL")
    with psycopg.connect(base) as c:
        names = [r[0] for r in c.execute("SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1")]
    for name in names:
        try:
            with psycopg.connect(urlsplit(base)._replace(path=f"/{name}").geturl(), autocommit=True) as c:
                c.execute("SET default_transaction_read_only = on")
                size, ins, upd, dele = c.execute(
                    "SELECT pg_database_size(current_database()), coalesce(sum(n_tup_ins), 0),"
                    " coalesce(sum(n_tup_upd), 0), coalesce(sum(n_tup_del), 0) FROM pg_stat_user_tables").fetchone()  # type: ignore[misc]
            print(f"db {name} size={size} user_inserted={ins} user_updated={upd} user_deleted={dele}")
        except psycopg.Error as e:
            print(f"db {name} not readable with this user ({type(e).__name__})")


def crash(config_path: str, entity: str, chunk_no: str) -> None:
    """Run generate against ECOMMERCE_DATABASE_URL (must be a namespace sibling) and SIGKILL this process just
    before writing (entity, chunk_no): a real hard crash."""
    url = env_url("ECOMMERCE_DATABASE_URL")
    check_name(dbname_of(url), sibling=True)
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
    "url": lambda name: print(db_url(name)), "exists": lambda name: sys.exit(0 if exists(name) else 1),
    "create": create, "drop": drop, "system-id": system_id, "wal-lsn": wal_lsn, "wal-bytes-since": wal_bytes_since,
    "sizes": sizes, "digest": digest, "pg-stats": pg_stats, "crash": crash,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(f"usage: homelab_bench.py {{{'|'.join(COMMANDS)}}} [args]")
    COMMANDS[sys.argv[1]](*sys.argv[2:])
