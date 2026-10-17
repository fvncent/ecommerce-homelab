import argparse
import sys

from ecomgen import __version__, config


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ecomgen", description="Synthetic e-commerce source-data generator")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("check-config", help="validate a config file and required environment variables")
    p.add_argument("config", help="path to generator YAML")
    args = parser.parse_args(argv)
    if args.command == "check-config":
        return check_config(args.config)
    parser.print_help()
    return 0
