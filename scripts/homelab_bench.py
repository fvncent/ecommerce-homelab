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
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import psycopg
from psycopg import sql

if TYPE_CHECKING:  # the package is imported lazily, inside the commands that need it
    from ecomgen import config, replay

TEST_DB, SMOKE_DB, CRASH_DB = "ecommerce_homelab_test", "ecommerce_homelab_smoke", "ecommerce_homelab_crash"
DATABASES = (TEST_DB, SMOKE_DB, CRASH_DB)
TABLES = ("sellers", "categories", "customers", "products", "orders", "order_items", "payment_attempts")
PRIVILEGES = ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls")
PC_SUFFIX = "_pc"  # schemas of the publish-crash run: an isolated, empty schema set in the smoke database
FORBIDDEN_PORTS = {55432: "the VLSIM PostgreSQL container"}
LOOPBACK = ("127.0.0.1", "localhost", "::1")
SAFE_PARAMS = {"sslmode", "connect_timeout", "application_name"}  # anything else (host, hostaddr, service, port,
# options, ...) could make libpq connect somewhere other than the URL's host and port
REDIRECTING_ENV = ("PGHOST", "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")
FAKE_RUN = "run_20000101T000000Z_000000"  # file names of the feed-only throughput runs


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
    if "dbname" in parse_qs(parts.query, keep_blank_values=True) or "%" in parts.path:  # empty dbname = user name
        raise Refused("URL must name its database only in the path (no ?dbname=, no percent-encoding)")
    return parts.path.lstrip("/")


def check_name(name: str, allowed: tuple[str, ...] = DATABASES) -> str:
    if name not in allowed:
        raise Refused(f"database {name!r} is not an approved benchmark database ({', '.join(allowed)})")
    return name


def check_server(url: str) -> str:
    """Refuse, before connecting, any URL that could reach a server other than the local one on the expected port:
    host must be loopback, the port explicit, allowed and equal to EXPECTED_PG_PORT (if set), only SAFE_PARAMS in
    the query, and no PGHOST/PGHOSTADDR/PGSERVICE in the environment."""
    parts = urlsplit(url)
    if parts.hostname not in LOOPBACK:
        raise Refused(f"the benchmark connects only to a local PostgreSQL, not {parts.hostname!r}")
    extra = set(parse_qs(parts.query, keep_blank_values=True)) - SAFE_PARAMS
    if extra:
        raise Refused(f"URL parameters {', '.join(sorted(extra))} are not allowed (only {', '.join(sorted(SAFE_PARAMS))})")
    set_env = [v for v in REDIRECTING_ENV if os.environ.get(v)]
    if set_env:
        raise Refused(f"unset {', '.join(set_env)}: they can redirect the connection")
    if parts.port is None:
        raise Refused("the URL must name its port explicitly")
    if parts.port in FORBIDDEN_PORTS:
        raise Refused(f"port {parts.port} is {FORBIDDEN_PORTS[parts.port]}; never benchmark against it")
    expected = os.environ.get("EXPECTED_PG_PORT")
    if expected and int(expected) != parts.port:
        raise Refused(f"URL port is {parts.port}, expected {expected}")
    return url


def bench_url() -> str:
    url = check_server(env_url("ECOMGEN_BENCH_URL"))
    check_name(dbname_of(url))
    return url


def db_url(name: str) -> str:
    return urlsplit(bench_url())._replace(path=f"/{check_name(name)}").geturl()


def _schemas() -> tuple[str, ...]:
    from ecomgen import config
    pg = config.load(os.environ.get("CONFIG", "config/generator.example.yaml")).postgres
    return pg.application_schema, pg.integration_schema, pg.metadata_schema


def preflight() -> None:
    """Refuse unless the server is local and not a forbidden port, and the role is unprivileged and owns every
    approved database, each still empty (also of the publish-crash schema set)."""
    bench_url()  # host, port and parameters are checked before any connection
    schemas = _schemas() + tuple(s + PC_SUFFIX for s in _schemas())
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
            member_of = [r for (r,) in c.execute(
                "SELECT pg_get_userbyid(roleid) FROM pg_auth_members WHERE member = to_regrole(current_user)")]
            if member_of:  # e.g. pg_write_all_data, or another owner role: privileges beyond our databases
                raise Refused(f"role {user} is a member of {', '.join(member_of)}; it must belong to no role")
            others = [d for (d,) in c.execute(
                "SELECT datname FROM pg_database WHERE datdba = to_regrole(current_user) AND datname <> ALL(%s)",
                (list(DATABASES),))]
            if others:
                raise Refused(f"role {user} also owns {', '.join(others)}; it must own only {', '.join(DATABASES)}")
            present = [s for (s,) in c.execute("SELECT nspname FROM pg_namespace WHERE nspname = ANY(%s)",
                                               (list(schemas),))]
            if present:
                raise Refused(f"database {name} is not empty (schemas {', '.join(present)} exist); the benchmark "
                              "never reuses a database: recreate it by hand (approved cleanup) first")
            version = c.execute("SHOW server_version").fetchone()[0]  # type: ignore[index]
            port = c.execute("SELECT inet_server_port()").fetchone()[0]  # type: ignore[index]
            if port in FORBIDDEN_PORTS:  # the server's own port (a published container reports its inner port)
                raise Refused(f"server port {port} is {FORBIDDEN_PORTS[port]}; never benchmark against it")
    print(f"role {user} (no privileges, no memberships) owns only {', '.join(DATABASES)}, all empty; "
          f"PostgreSQL {version}, port {port}")


def same_server(kind: str, ref: str) -> None:
    """Exit 1 unless the server behind ECOMGEN_BENCH_URL is the one being measured: our backend's PID is in the
    systemd unit's cgroup (`cgroup PATH`) or is a postgres process inside the container (`container ID`)."""
    with psycopg.connect(bench_url()) as c:
        pid = c.execute("SELECT pg_backend_pid()").fetchone()[0]  # type: ignore[index]
        if kind == "cgroup":
            with open(os.path.join(ref, "cgroup.procs")) as f:
                ok = str(pid) in f.read().split()
        elif kind == "container":
            import subprocess
            cmd = subprocess.run(["docker", "exec", ref, "cat", f"/proc/{pid}/cmdline"], capture_output=True)
            ok = cmd.returncode == 0 and b"postgres" in cmd.stdout
        else:
            raise Refused(f"unknown server kind {kind!r}")
    print(f"backend {pid} {'is' if ok else 'is NOT'} in {kind} {ref}")
    sys.exit(0 if ok else 1)


def server_epoch() -> None:
    """The server's clock (seconds), so 'created by this run' compares server time with server time."""
    with psycopg.connect(bench_url()) as c:
        print(int(c.execute("SELECT extract(epoch FROM now())").fetchone()[0]))  # type: ignore[index]


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
                " AND backend_type = 'client backend' AND pid <> pg_backend_pid()),"  # not autovacuum workers
                " (SELECT count(*) FROM pg_prepared_xacts"
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
    tables only: autovacuum/autoanalyze write the system catalogs, so pg_stat_database counters move when idle.
    Settings are those the URL's role may see (all only with superuser or pg_read_all_settings); for the homelab's
    host cluster host_snapshot.sh reads the same as the postgres superuser instead (PG_LOCAL_SUPERUSER=1)."""
    urls = os.environ.get("EXISTING_PG_URL", "").split()
    if not urls:
        raise Refused("EXISTING_PG_URL is not set")
    for base in urls:
        dbname_of(base)
    for base in urls:
        parts = urlsplit(base)
        print(f"server {parts.hostname}:{parts.port or 5432}")
        try:
            with psycopg.connect(base) as c:
                names = [r[0] for r in c.execute("SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1")]
                roles = [r[0] for r in c.execute("SELECT rolname FROM pg_roles WHERE rolname !~ '^pg_' ORDER BY 1")]
                settings = c.execute("SELECT name, setting FROM pg_settings WHERE source NOT IN ('default',"
                                     " 'override', 'client', 'session') ORDER BY 1").fetchall()
        except psycopg.Error as e:
            print(f"server not readable with this URL ({type(e).__name__})")
            continue
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
    url = check_server(env_url("ECOMMERCE_DATABASE_URL"))
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


# ---------- files (Phase 2) ----------

def _cfg() -> "config.Config":
    from ecomgen import config
    return config.load(os.environ.get("CONFIG", "config/generator.example.yaml"))


def pc_config(src: str, dst: str) -> None:
    """Write a copy of config src whose three schemas carry PC_SUFFIX; refuses to overwrite dst."""
    import yaml
    doc = yaml.safe_load(Path(src).read_text())
    for k in ("application_schema", "integration_schema", "metadata_schema"):
        doc["postgres"][k] += PC_SUFFIX
    out = Path(dst)
    if out.exists():
        raise Refused(f"{dst} already exists")
    out.write_text(yaml.safe_dump(doc, sort_keys=False))
    print(dst)


def _latest(c: psycopg.Connection, cfg: "config.Config") -> str:
    from ecomgen import db
    return db.resolve_run(c, cfg, None)


def run_id(name: str) -> None:
    with psycopg.connect(db_url(name)) as c:
        print(_latest(c, _cfg()))


def feed_digest(name: str) -> None:
    """One line per published feed file of the database's latest run: path (run id -> RUN), sha256, records;
    sorted. Equal output = identical files, comparable across machines."""
    from ecomgen import db
    cfg = _cfg()
    with psycopg.connect(db_url(name)) as c:
        rid = _latest(c, cfg)
        rows = c.execute(db.q(cfg, "SELECT rel_path, sha256, records FROM {meta}.published_files"
                                   " WHERE run_id = %s AND feed <> 'manifest'"), (rid,)).fetchall()
    for line in sorted(f"{p.replace(rid, 'RUN')} {s} {n}" for p, s, n in rows):
        print(line)


def feed_stats(name: str) -> None:
    from ecomgen import db
    cfg = _cfg()
    with psycopg.connect(db_url(name)) as c:
        rid = _latest(c, cfg)
        for feed, files, records, size in c.execute(db.q(cfg, "SELECT feed, count(*), sum(records), sum(bytes)"
                                                              " FROM {meta}.published_files WHERE run_id = %s"
                                                              " GROUP BY 1 ORDER BY 1"), (rid,)):
            print(f"{feed} files={files} records={records} bytes={size}")


def verify_files(name: str) -> None:
    """Re-hash every recorded file of the latest run; also count unrecorded files carrying its run id where it
    publishes (orphans) and leftover staging. Exit 1 on any problem."""
    from ecomgen import config, db, feeds
    cfg = _cfg()
    root = Path(config.data_root(cfg))
    with psycopg.connect(db_url(name)) as c:
        rid = _latest(c, cfg)
        rows = c.execute(db.q(cfg, "SELECT rel_path, sha256, bytes FROM {meta}.published_files"
                                   " WHERE run_id = %s"), (rid,)).fetchall()
    started = time.perf_counter()
    bad = [r for r, sha, _ in rows if not (root / r).is_file() or feeds.sha256_of(root / r) != sha]
    seconds = time.perf_counter() - started
    total = sum(int(b) for _, _, b in rows)
    own = [root / "source_feeds", root / "clickstream", root / "manifests" / f"run_id={rid}"]  # only where it publishes
    on_disk = {str(p.relative_to(root)) for d in own if d.is_dir() for p in d.rglob(f"*{rid}*") if p.is_file()}
    if (root / feeds.manifest_path(rid)).is_file():
        on_disk.add(feeds.manifest_path(rid))
    orphans = on_disk - {r for r, _, _ in rows}
    staging_left = (root / "staging" / rid).exists()
    print(f"files={len(rows)} bytes={total} seconds={seconds:.2f} mb_per_s={total / 1048576 / max(seconds, 1e-6):.0f}"
          f" mismatches={len(bad)} orphans={len(orphans)} staging_left={int(staging_left)}")
    for r in sorted(bad)[:5] + sorted(orphans)[:5]:
        print(f"  problem: {r}")
    sys.exit(1 if bad or orphans or staging_left else 0)


def feed_throughput(config_path: str, outdir: str, feed: str) -> None:
    """Write one feed alone (config counts, no database) into outdir/feed; print records, bytes and rates."""
    from ecomgen import config, feeds, generate
    if feed not in ("payment_settlement", "clickstream"):
        raise Refused(f"unknown feed {feed!r}")
    target = Path(outdir) / feed
    if target.exists():
        raise Refused(f"{target} already exists")
    cfg = config.load(config_path)
    one = replace(cfg, filesystem=replace(cfg.filesystem, csv=feed == "payment_settlement",
                                          jsonl=feed == "clickstream"))
    started = time.perf_counter()
    stats = feeds.write_staged(generate.World.from_config(cfg), one, FAKE_RUN, target)
    seconds = time.perf_counter() - started
    records = sum(s.records for s in stats.values())
    size = sum((target / rel).stat().st_size for rel in stats)
    print(f"{feed} files={len(stats)} records={records} bytes={size} seconds={seconds:.2f}"
          f" records_per_s={records / seconds:.0f} mb_per_s={size / 1048576 / seconds:.1f}")


def _kill_after_placing(n: int) -> None:
    """From now on, SIGKILL this process right after the nth file is placed (before its ledger row commits)."""
    from ecomgen import feeds
    real, placed = feeds.place_file, [0]

    def killing(*args: object, **kwargs: object) -> bool:
        result = real(*args, **kwargs)  # type: ignore[arg-type]
        placed[0] += 1
        if placed[0] == n:
            os.kill(os.getpid(), signal.SIGKILL)
        return result
    feeds.place_file = killing


def publish_crash(config_path: str, nth: str) -> None:
    """generate --profile smoke against ECOMMERCE_DATABASE_URL (must be the smoke database), SIGKILLed right after
    placing the nth file: a hard crash between the filesystem and the PostgreSQL commit."""
    url = check_server(env_url("ECOMMERCE_DATABASE_URL"))
    check_name(dbname_of(url), (SMOKE_DB,))
    from ecomgen import config, load
    _kill_after_placing(int(nth))
    with psycopg.connect(url, autocommit=True) as conn:
        load.run(conn, config.load(config_path), "smoke")


def _replay_params(start: str, end: str, delay: str, jitter: str, dup: str) -> "replay.Params":
    from ecomgen import feeds, replay
    return replay.Params(date.fromisoformat(start), date.fromisoformat(end), tuple(feeds.enabled(_cfg())),
                         int(delay), int(jitter), int(dup))


def replay_crash(config_path: str, nth: str, start: str, end: str, delay: str, jitter: str, dup: str) -> None:
    """replay-files against ECOMMERCE_DATABASE_URL (must be the test database), SIGKILLed right after placing the
    nth delivery."""
    url = check_server(env_url("ECOMMERCE_DATABASE_URL"))
    check_name(dbname_of(url), (TEST_DB,))
    from ecomgen import config, replay
    params = _replay_params(start, end, delay, jitter, dup)
    _kill_after_placing(int(nth))
    with psycopg.connect(url, autocommit=True) as conn:
        replay.replay(conn, config.load(config_path), params)


def replay_id(name: str, start: str, end: str, delay: str, jitter: str, dup: str) -> None:
    from ecomgen import replay
    with psycopg.connect(db_url(name)) as c:
        print(replay.replay_id(_latest(c, _cfg()), _replay_params(start, end, delay, jitter, dup)))


def replay_digest(name: str, rid: str) -> None:
    """One line per delivered file of the replay: path (replay id -> RPL, run id -> RUN), sha256, arrival,
    duplicate; sorted. The delivery manifest is excluded (it names the ids)."""
    from ecomgen import db, feeds
    cfg = _cfg()
    with psycopg.connect(db_url(name)) as c:
        run = _latest(c, cfg)
        rows = c.execute(db.q(cfg, "SELECT delivered_path, sha256, arrived_at, duplicate FROM {meta}.file_deliveries"
                                   " WHERE replay_id = %s AND original_path IS NOT NULL"), (rid,)).fetchall()
    for line in sorted(f"{p.replace(rid, 'RPL').replace(run, 'RUN')} {s} {feeds.ts(a)} {int(d)}"
                       for p, s, a, d in rows):
        print(line)


def planned_replay_digest(name: str, start: str, end: str, delay: str, jitter: str, dup: str) -> None:
    """What an uninterrupted replay with these parameters delivers, from the latest run's verified originals and
    the pure schedule, in replay-digest's format (nothing is written)."""
    from ecomgen import config, feeds, replay
    cfg = _cfg()
    params = _replay_params(start, end, delay, jitter, dup)
    with psycopg.connect(db_url(name)) as c:
        run = _latest(c, cfg)
        _, originals = replay.originals(c, cfg, run, Path(config.data_root(cfg)), params)
    rid = replay.replay_id(run, params)
    for line in sorted(f"{d.path.replace(rid, 'RPL').replace(run, 'RUN')} {d.original.sha256} {feeds.ts(d.arrived_at)}"
                       f" {int(d.duplicate)}" for d in replay.schedule(rid, originals, params)):
        print(line)


def replay_originals(name: str, start: str, end: str, delay: str, jitter: str, dup: str) -> None:
    """The originals a replay with these parameters covers: path (run id -> RUN) and sha256, sorted. Machine- and
    run-independent, unlike the schedule (seeded by the replay id, which includes the run id)."""
    from ecomgen import config, replay
    cfg = _cfg()
    with psycopg.connect(db_url(name)) as c:
        run = _latest(c, cfg)
        _, originals = replay.originals(c, cfg, run, Path(config.data_root(cfg)),
                                         _replay_params(start, end, delay, jitter, dup))
    for line in sorted(f"{o.path.replace(run, 'RUN')} {o.sha256}" for o in originals):
        print(line)


def verify_replay(name: str, rid: str) -> None:
    """Re-hash every recorded, not-deleted delivery; count unrecorded files in the replay's directory and leftover
    staging. Exit 1 on any problem."""
    from ecomgen import config, db, feeds
    cfg = _cfg()
    root = Path(config.data_root(cfg))
    with psycopg.connect(db_url(name)) as c:
        rows = c.execute(db.q(cfg, "SELECT delivered_path, sha256 FROM {meta}.file_deliveries"
                                   " WHERE replay_id = %s AND deleted_at IS NULL"), (rid,)).fetchall()
    bad = [p for p, sha in rows if not (root / p).is_file() or feeds.sha256_of(root / p) != sha]
    base = root / "deliveries" / f"replay_id={rid}"
    on_disk = {str(p.relative_to(root)) for p in base.rglob("*") if p.is_file()} if base.is_dir() else set()
    orphans = on_disk - {p for p, _ in rows}
    staging_left = (root / "staging" / rid).exists()
    print(f"deliveries={len(rows)} mismatches={len(bad)} orphans={len(orphans)} staging_left={int(staging_left)}")
    sys.exit(1 if bad or orphans or staging_left else 0)


def owned_replay(name: str, rid: str, run: str, since_epoch: str) -> None:
    """Exit 0 only if the replay is COMPLETED, replays this run, and started after since_epoch (i.e. this benchmark
    created it): the guard before the benchmark deletes anything."""
    from ecomgen import db
    cfg = _cfg()
    with psycopg.connect(db_url(name)) as c:
        row = c.execute(db.q(cfg, "SELECT run_id, state, started_at >= to_timestamp(%s) FROM {meta}.replays"
                                  " WHERE replay_id = %s"), (int(since_epoch), rid)).fetchone()
    ok = row is not None and row[0] == run and row[1] == "COMPLETED" and row[2]
    print(f"replay {rid}: {'created by this benchmark' if ok else 'NOT created by this benchmark'} ({row})")
    sys.exit(0 if ok else 1)


def link_test() -> None:
    """The data root must support hard links (publishing places files with os.link)."""
    root = Path(env_root())
    probe = root / "staging" / f".linktest-{os.getpid()}"
    probe.parent.mkdir(exist_ok=True)
    probe.write_text("x\n")
    try:
        os.link(probe, probe.with_suffix(".link"))
        probe.with_suffix(".link").unlink()
    finally:
        probe.unlink()
    print(f"{root}: hard links work")


def env_root() -> str:
    root = os.environ.get("ECOMGEN_DATA_ROOT", "")
    if not root or not os.path.isabs(root) or not os.path.isdir(root):
        raise Refused(f"ECOMGEN_DATA_ROOT must be an existing absolute directory, got {root!r}")
    return root


def feeds_enabled() -> None:
    from ecomgen import feeds
    on = feeds.enabled(_cfg())
    if set(on) != {"payment_settlement", "clickstream"}:
        raise Refused(f"the config must enable both feeds (filesystem.csv and .jsonl), enables {on}")
    print("feeds: " + " ".join(on))


COMMANDS: dict[str, Callable[..., None]] = {
    "url": lambda name: print(db_url(name)), "preflight": preflight, "same-server": same_server,
    "server-major": server_major, "server-epoch": server_epoch,
    "wal-lsn": wal_lsn, "wal-bytes-since": wal_bytes_since, "sizes": sizes, "digest": digest,
    "leftovers": leftovers, "pg-stats": pg_stats, "crash": crash,
    "pc-config": pc_config, "run-id": run_id, "feed-digest": feed_digest, "feed-stats": feed_stats,
    "verify-files": verify_files, "feed-throughput": feed_throughput, "publish-crash": publish_crash,
    "replay-crash": replay_crash, "replay-id": replay_id, "replay-digest": replay_digest,
    "planned-replay-digest": planned_replay_digest, "replay-originals": replay_originals, "verify-replay": verify_replay, "owned-replay": owned_replay, "link-test": link_test,
    "feeds-enabled": feeds_enabled,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(f"usage: homelab_bench.py {{{'|'.join(COMMANDS)}}} [args]")
    COMMANDS[sys.argv[1]](*sys.argv[2:])
