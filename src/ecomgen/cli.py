import argparse
import resource
import sys
import time

import psycopg

from ecomgen import __version__, config, db, generate, load


def check_config(path: str) -> int:
    try:
        cfg = config.load(path)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    g = cfg.generator
    print(f"config OK: {path} (sha256 {cfg.sha256[:12]})")
    print(f"  profile={g.profile} seed={g.seed} orders={g.counts['orders']} chunk_rows={g.chunk_rows} workers={g.workers}")
    print(f"  clock {g.clock_start.isoformat()} -> {g.clock_end.isoformat()}")
    try:
        config.database_url(cfg)
    except config.ConfigError as e:
        print(f"environment error: {e}", file=sys.stderr)
        return 2
    print(f"  {cfg.postgres.connection_url_env} is set")
    return 0


def init_db(path: str) -> int:
    try:
        cfg = config.load(path)
        url = config.database_url(cfg)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    try:
        with psycopg.connect(url) as conn:
            applied = db.migrate(conn, cfg)
    except db.MigrationError as e:
        print(f"migration error: {e}", file=sys.stderr)
        return 1
    except psycopg.Error as e:
        print(f"database error: {type(e).__name__}: {e}".rstrip(), file=sys.stderr)
        return 1
    print(f"applied: {', '.join(applied)}" if applied else "database schema is up to date")
    return 0


def generate_cmd(path: str, profile: str | None) -> int:
    try:
        cfg = config.load(path)
        url = config.database_url(cfg)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    started = time.perf_counter()
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            result = load.run(conn, cfg, profile)
    except (load.LoadError, generate.GenerationError) as e:
        print(f"generate error: {e}", file=sys.stderr)
        return 1
    except psycopg.Error as e:
        print(f"database error: {type(e).__name__}: {e}".rstrip(), file=sys.stderr)
        return 1
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = rss / 2**20 if sys.platform == "darwin" else rss / 2**10  # bytes on macOS, KiB on Linux
    print(f"run {result.run_id} COMPLETED: {result.chunks_written} chunks in {time.perf_counter() - started:.1f}s,"
          f" peak RSS {rss_mb:.0f} MB")
    print("  " + " ".join(f"{t}={n}" for t, n in result.counts.items()))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ecomgen", description="Synthetic e-commerce source-data generator")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("check-config", help="validate a config file and required environment variables")
    p.add_argument("config", help="path to generator YAML")
    p = sub.add_parser("init-db", help="create schemas and apply pending migrations")
    p.add_argument("config", help="path to generator YAML")
    p = sub.add_parser("generate", help="start a new run and load generated data into PostgreSQL")
    p.add_argument("config", help="path to generator YAML")
    p.add_argument("--profile", choices=sorted(load.PROFILE_COUNTS),
                   help="use built-in counts (smoke: 1,000 orders) instead of the config's counts")
    args = parser.parse_args(argv)
    if args.command == "check-config":
        return check_config(args.config)
    if args.command == "init-db":
        return init_db(args.config)
    if args.command == "generate":
        return generate_cmd(args.config, args.profile)
    parser.print_help()
    return 0
