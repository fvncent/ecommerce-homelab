import argparse
import datetime as dt
import resource
import sys
import time

import psycopg

from ecomgen import __version__, cleanup, config, db, feeds, generate, load, replay, validate


def _connect_cfg(path: str) -> tuple[config.Config, str] | int:
    try:
        cfg = config.load(path)
        return cfg, config.database_url(cfg)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2


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
        root = config.data_root(cfg)
        print(f"  files: {', '.join(feeds.enabled(cfg)) or 'none'} under {root}")
        config.database_url(cfg)
    except config.ConfigError as e:
        print(f"environment error: {e}", file=sys.stderr)
        return 2
    print(f"  {cfg.postgres.connection_url_env} is set")
    return 0


def init_db(path: str) -> int:
    loaded = _connect_cfg(path)
    if isinstance(loaded, int):
        return loaded
    cfg, url = loaded
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
    loaded = _connect_cfg(path)
    if isinstance(loaded, int):
        return loaded
    cfg, url = loaded
    started = time.perf_counter()
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            result = load.run(conn, cfg, profile)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except (load.LoadError, generate.GenerationError, feeds.FeedError) as e:
        print(f"generate error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"file error: {e}; if a run was started it is now FAILED (see `ecomgen status`): fix the cause and "
              "run `ecomgen resume`", file=sys.stderr)
        return 1
    except psycopg.Error as e:
        print(f"database error: {type(e).__name__}: {e}".rstrip(), file=sys.stderr)
        return 1
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = rss / 2**20 if sys.platform == "darwin" else rss / 2**10  # bytes on macOS, KiB on Linux
    print(f"run {result.run_id} COMPLETED: {result.chunks_written} chunks in {time.perf_counter() - started:.1f}s,"
          f" peak RSS {rss_mb:.0f} MB")
    print("  " + " ".join(f"{t}={n}" for t, n in result.counts.items()))
    _print_files(result.files)
    return 0


def advance_cmd(path: str, to: str) -> int:
    """Exit 0: step completed (or the clock was already there). 1: refused or failed. 2: config/argument error."""
    loaded = _connect_cfg(path)
    if isinstance(loaded, int):
        return loaded
    cfg, url = loaded
    try:
        as_of = None if to == "end" else dt.datetime.fromisoformat(to)
    except ValueError:
        print(f"argument error: --to {to!r} is not an ISO timestamp (e.g. 2026-01-08T00:00:00Z) or 'end'",
              file=sys.stderr)
        return 2
    if as_of is not None:
        if as_of.utcoffset() is None:
            print(f"argument error: --to {to!r} has no time zone; add Z for UTC", file=sys.stderr)
            return 2
        as_of = as_of.astimezone(dt.UTC)
    started = time.perf_counter()
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            result = load.advance(conn, cfg, as_of)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except (load.LoadError, generate.GenerationError) as e:
        print(f"advance error: {e}", file=sys.stderr)
        return 1
    except psycopg.Error as e:
        print(f"database error: {type(e).__name__}: {e}".rstrip(), file=sys.stderr)
        return 1
    if result is None:
        print(f"the clock is already at {to}; nothing to do")
        return 0
    _print_step(result, f"{result.chunks_written} chunks in {time.perf_counter() - started:.1f}s")
    return 0


def _print_step(r: load.RunResult, timing: str) -> None:
    start = r.previous_as_of.isoformat() if r.previous_as_of else "start"
    assert r.as_of is not None
    print(f"step {r.run_id} COMPLETED: clock {start} -> {r.as_of.isoformat()}, {timing}")
    print("  inserted " + " ".join(f"{t}={n}" for t, n in r.counts.items() if n))
    print("  updated " + " ".join(f"{t}={n}" for t, n in r.updated.items()))


def _print_files(p: feeds.Published) -> None:
    if p.files:
        print(f"  files: {p.files} recorded ({p.newly_published} newly published); "
              + " ".join(f"{f}={n}" for f, n in p.records.items()))


def validate_cmd(path: str, run_id: str | None) -> int:
    """Exit 0: all checks pass. 1: a check failed (bad data). 2: config error. 3: could not validate."""
    loaded = _connect_cfg(path)
    if isinstance(loaded, int):
        return loaded
    cfg, url = loaded
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            run_id, results = validate.validate(conn, cfg, run_id)
    except (validate.ValidateError, db.RunLookupError, OSError) as e:
        print(f"validate error: {e}", file=sys.stderr)
        return 3
    except psycopg.Error as e:
        print(f"database error: {type(e).__name__}: {e}".rstrip(), file=sys.stderr)
        return 3
    print(f"run {run_id}")
    for r in results:
        if r.ok:
            print(f"  OK   {r.check.name}")
        else:
            print(f"  FAIL {r.check.name}: {r.violations} violation(s) — {r.check.description}")
            for sample in r.samples:
                print(f"         {sample}")
    failed = sum(not r.ok for r in results)
    print(f"{len(results) - failed} passed, {failed} failed")
    return 1 if failed else 0


def status_cmd(path: str, run_id: str | None) -> int:
    """Exit 0: status shown. 2: config error. 3: could not read status (no run, run not found, DB error)."""
    loaded = _connect_cfg(path)
    if isinstance(loaded, int):
        return loaded
    cfg, url = loaded
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            st = load.status(conn, cfg, run_id)
    except db.RunLookupError as e:
        print(f"status error: {e}", file=sys.stderr)
        return 3
    except psycopg.Error as e:
        print(f"database error: {type(e).__name__}: {e}".rstrip(), file=sys.stderr)
        return 3
    print(f"run {st.run_id}: {st.state} (profile {st.profile}, generator {st.generator_version})")
    if st.as_of is not None:
        start = st.previous_as_of.isoformat() if st.previous_as_of else "start"
        print(f"  clock step: {start} -> {st.as_of.isoformat()} (orders = placed or changed in this step)")
    print(f"  started {st.started_at.isoformat()}" + (f", finished {st.finished_at.isoformat()}" if st.finished_at else ""))
    print("  progress: " + ", ".join(f"{e} {d}/{p}" for e, (d, p) in st.progress.items()))
    if st.error:
        print(f"  last error: {st.error}")
    if st.resume_mismatch and st.state != "COMPLETED":
        print(f"  note: resume would refuse; differs from the run: {', '.join(st.resume_mismatch)}")
    if st.writer_pid is not None:
        print(f"  backend pid {st.writer_pid} holds the run lock (a live generate/resume, or a dead client's"
              " lingering session)")
    elif st.state == "RUNNING":
        print("  no process is writing: the run crashed; run `ecomgen resume`")
    elif st.state == "FAILED":
        print("  run `ecomgen resume` to finish it")
    return 0


def resume_cmd(path: str, run_id: str | None) -> int:
    """Exit 0: completed (or already was). 1: resume failed or refused. 2: config error. 3: no run / run not found."""
    loaded = _connect_cfg(path)
    if isinstance(loaded, int):
        return loaded
    cfg, url = loaded
    started = time.perf_counter()
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            result = load.resume(conn, cfg, run_id)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except db.RunLookupError as e:
        print(f"resume error: {e}", file=sys.stderr)
        return 3
    except (load.LoadError, generate.GenerationError, feeds.FeedError) as e:
        print(f"resume error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"file error: {e}; fix the cause and run `ecomgen resume` again", file=sys.stderr)
        return 1
    except psycopg.Error as e:
        print(f"database error: {type(e).__name__}: {e}".rstrip(), file=sys.stderr)
        return 1
    if result is None:
        print("run is already COMPLETED; nothing to resume")
        return 0
    if result.as_of is not None:
        _print_step(result, f"{result.chunks_written} chunks written, {result.chunks_skipped} already committed, in"
                            f" {time.perf_counter() - started:.1f}s")
        return 0
    print(f"run {result.run_id} COMPLETED: {result.chunks_written} chunks written, {result.chunks_skipped} already"
          f" committed, in {time.perf_counter() - started:.1f}s")
    print("  " + " ".join(f"{t}={n}" for t, n in result.counts.items()))
    _print_files(result.files)
    return 0


def replay_cmd(path: str, run_id: str | None, start: str, end: str, feed: list[str] | None, delay: int,
               jitter: int, duplicate_pct: int) -> int:
    """Exit 0: delivered (or verified). 1: refused or failed. 2: config or argument error. 3: no run / not found."""
    loaded = _connect_cfg(path)
    if isinstance(loaded, int):
        return loaded
    cfg, url = loaded
    try:
        params = replay.Params(dt.date.fromisoformat(start), dt.date.fromisoformat(end),
                               tuple(sorted(set(feed or feeds.enabled(cfg)))), delay, jitter, duplicate_pct)
    except (ValueError, replay.ReplayError) as e:
        print(f"replay error: {e}", file=sys.stderr)
        return 2
    started = time.perf_counter()
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            r = replay.replay(conn, cfg, params, run_id)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except db.RunLookupError as e:
        print(f"replay error: {e}", file=sys.stderr)
        return 3
    except (replay.ReplayError, feeds.FeedError, load.LoadError) as e:
        print(f"replay error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"file error: {e}; fix the cause and repeat the same command to finish the replay", file=sys.stderr)
        return 1
    except psycopg.Error as e:
        print(f"database error: {type(e).__name__}: {e}".rstrip(), file=sys.stderr)
        return 1
    what = "verified (already complete)" if r.verified_only else f"{r.newly_delivered} newly delivered"
    print(f"replay {r.replay_id} of run {r.run_id}: {r.deliveries} deliveries ({r.duplicates} duplicates), {what},"
          f" in {time.perf_counter() - started:.1f}s")
    print(f"  under {config.data_root(cfg)}/{replay.delivery_manifest_path(r.replay_id).rsplit('/', 1)[0]}")
    return 0


def cleanup_cmd(path: str, target: cleanup.Target, delete: bool, confirm: str | None) -> int:
    """Exit 0: dry run shown, or deleted. 1: refused or failed. 2: config/argument error. 3: run/replay not found."""
    if delete != (confirm is not None):
        print("cleanup error: --delete and --confirm TOKEN go together (run without them first for the token)",
              file=sys.stderr)
        return 2
    loaded = _connect_cfg(path)
    if isinstance(loaded, int):
        return loaded
    cfg, url = loaded
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            plan, result = cleanup.run(conn, cfg, target, confirm)
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except db.RunLookupError as e:
        print(f"cleanup error: {e}", file=sys.stderr)
        return 3
    except (cleanup.CleanupError, feeds.FeedError, load.LoadError) as e:
        print(f"cleanup error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"file error: {e}; repeat the dry run to see what is left", file=sys.stderr)
        return 1
    except psycopg.Error as e:
        print(f"database error: {type(e).__name__}: {e}".rstrip(), file=sys.stderr)
        return 1
    if result is None:
        what = f"{target.kind} {target.ident}" if target.ident else "orphan staging directories"
        total = sum(c.bytes for c in plan.candidates)
        print(f"dry run: cleanup of {what} under {plan.root} would delete {len(plan.candidates)} item(s),"
              f" {total} bytes:")
        for c in plan.candidates:
            print(f"  delete {c.path} ({c.bytes} bytes)")
        for k in plan.kept:
            print(f"  keep   {k}")
        if plan.candidates:
            print(f"to delete exactly these, add: --delete --confirm {plan.token}")
        return 0
    if result.cleanup_id is None:
        print("nothing to delete")
        return 0
    print(f"cleanup {result.cleanup_id}: deleted {result.deleted} file(s), {result.bytes} bytes;"
          f" {result.already_gone} already gone; audit {plan.root}/{result.audit_path}")
    for rel in result.kept_changed:
        print(f"  kept {rel}: changed since it was recorded (not ours to delete)")
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
    p = sub.add_parser("advance", help="move the simulation clock forward: add the orders placed since the last "
                                       "step and update the ones that changed",
                       description="Each step is a run in the same schema set: it inserts the orders placed up to "
                                   "--to and updates earlier orders and payments whose state changed. Stepping to "
                                   "the end in any number of steps gives exactly the rows `generate` writes. Needs "
                                   "an empty schema set for the first step (not one holding a generate run) and a "
                                   "config with file feeds off. A crashed step is finished with `resume`. Exit codes: "
                                   "0 done (or already there), 1 refused or failed, 2 config/argument error.")
    p.add_argument("config", help="path to generator YAML (the same for every step)")
    p.add_argument("--to", required=True,
                   help="new clock time, ISO 8601 with a time zone (2026-01-08T00:00:00Z), or 'end' for clock_end")
    p = sub.add_parser("validate", help="check cross-row invariants and run-ledger completeness",
                       description="Exit codes: 0 all checks pass, 1 a check failed, 2 config error, 3 could not "
                                   "validate (no run, run not found, database error). Data checks cover the whole "
                                   "application schema; --run-id scopes the ledger checks.")
    p.add_argument("config", help="path to generator YAML")
    p.add_argument("--run-id", help="run to check (default: the most recent run)")
    p = sub.add_parser("status", help="show a run's state and chunk progress (read-only)")
    p.add_argument("config", help="path to generator YAML")
    p.add_argument("--run-id", help="run to show (default: the most recent run)")
    p = sub.add_parser("resume", help="finish a crashed (RUNNING) or FAILED run; committed chunks are skipped")
    p.add_argument("config", help="path to generator YAML (must be identical to the run's)")
    p.add_argument("--run-id", help="run to resume (default: the most recent run)")
    p = sub.add_parser("replay-files", help="deliver copies of a run's files for a date range (simulated arrival)",
                       description="Copies published files byte for byte under deliveries/replay_id=<id>/, with "
                                   "deterministic arrival times and optional duplicates, plus a delivery manifest. "
                                   "The same arguments always mean the same replay: repeating a command finishes "
                                   "or verifies it. Originals are never modified. Exit codes: 0 done, 1 refused or "
                                   "failed, 2 config/argument error, 3 no run / run not found.")
    p.add_argument("config", help="path to generator YAML")
    p.add_argument("--from", dest="start", required=True, help="first partition date, YYYY-MM-DD")
    p.add_argument("--to", dest="end", required=True, help="last partition date, YYYY-MM-DD (inclusive)")
    p.add_argument("--run-id", help="run whose files to replay (default: the most recent run)")
    p.add_argument("--feed", action="append", choices=("payment_settlement", "clickstream"),
                   help="feed to replay (repeatable; default: every feed the config enables)")
    p.add_argument("--delay-hours", type=int, default=24, help="hours after a partition's day ends (default 24)")
    p.add_argument("--jitter-hours", type=int, default=0, help="up to this many extra hours per file (default 0)")
    p.add_argument("--duplicate-pct", type=int, default=0,
                   help="percent of files delivered a second time, 1-24 h later (default 0)")
    p = sub.add_parser("cleanup-files", help="delete generated files (dry run unless --delete --confirm TOKEN)",
                       description="Deletes only files the database recorded as produced by the generator: a "
                                   "COMPLETED run's feed files, a COMPLETED replay's delivered copies, or staging "
                                   "directories of known runs/replays. Manifests and audit reports are kept. The "
                                   "dry run (default) prints a token for exactly its candidate list. Exit codes: 0 "
                                   "dry run or deleted, 1 refused/failed, 2 config/argument error, 3 not found.")
    p.add_argument("config", help="path to generator YAML")
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("--staging", action="store_true", help="orphan staging directories of known runs/replays")
    which.add_argument("--replay-id", help="a COMPLETED replay's delivered copies")
    which.add_argument("--run-id", help="a COMPLETED run's published feed files (validate then reports them)")
    p.add_argument("--delete", action="store_true", help="actually delete (needs --confirm)")
    p.add_argument("--confirm", metavar="TOKEN", help="token printed by the dry run")
    args = parser.parse_args(argv)
    if args.command == "check-config":
        return check_config(args.config)
    if args.command == "init-db":
        return init_db(args.config)
    if args.command == "generate":
        return generate_cmd(args.config, args.profile)
    if args.command == "advance":
        return advance_cmd(args.config, args.to)
    if args.command == "validate":
        return validate_cmd(args.config, args.run_id)
    if args.command == "status":
        return status_cmd(args.config, args.run_id)
    if args.command == "resume":
        return resume_cmd(args.config, args.run_id)
    if args.command == "cleanup-files":
        try:
            target = (cleanup.Target("staging") if args.staging else
                      cleanup.Target("replay", args.replay_id) if args.replay_id is not None else
                      cleanup.Target("run", args.run_id))
        except cleanup.CleanupError as e:
            print(f"cleanup error: {e}", file=sys.stderr)
            return 2
        return cleanup_cmd(args.config, target, args.delete, args.confirm)
    if args.command == "replay-files":
        return replay_cmd(args.config, args.run_id, args.start, args.end, args.feed, args.delay_hours,
                          args.jitter_hours, args.duplicate_pct)
    parser.print_help()
    return 0
