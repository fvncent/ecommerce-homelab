"""Cross-row invariants the schema can't enforce, plus run-ledger completeness.

Each check is a query returning one `id` per violation; only the count and a few sample IDs come back.
Scope: the application schema holds one generate run, or one chain of clock steps (`advance`); data checks cover
every row there (the state as of the latest step), run_completed and ledger_complete are scoped to the run, and
ledger_matches_tables sums the ledger of every run in the schema set (the chain's inserts add up to the tables).

File checks (runs that publish feeds): every recorded file is intact and listed in the manifest, and each
published feed agrees with PostgreSQL both ways (the Phase 2 gate). For a chain of clock steps the files of every
step are checked together against the current tables (each step holds only its window's events), so a record that
two steps both published, or that none did, is a violation. Feed rows are streamed into temporary tables
inside the same snapshot as the database checks, checked with the same id-per-violation queries, then dropped.
"""

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from ecomgen import db, feeds, generate, load
from ecomgen.config import Config

SAMPLE = 5


class ValidateError(RuntimeError):
    pass


@dataclass(frozen=True)
class Check:
    name: str
    description: str
    query: str  # returns column `id`; {app}/{meta} schemas, %(run)s run ID


@dataclass(frozen=True)
class Result:
    check: Check
    violations: int
    samples: list[str]

    @property
    def ok(self) -> bool:
        return self.violations == 0


LEDGER_CHECKS = (
    Check("run_completed", "run is COMPLETED", """
        SELECT run_id || ' is ' || state AS id FROM {meta}.generation_runs
        WHERE run_id = %(run)s AND state <> 'COMPLETED'"""),
    Check("ledger_complete", "ledger chunks are exactly the planned entities, rows and contiguous chunk numbers", """
        SELECT coalesce(p.key, c.entity) || ': planned ' || coalesce(p.value, 'none') || ', ledger '
               || coalesce(c.n, 0) || ' rows in ' || coalesce(c.chunks, 0) || ' chunks (max chunk_no '
               || coalesce(c.max_no, -1) || ')' AS id
        FROM (SELECT key, value FROM {meta}.generation_runs, jsonb_each_text(counts->'planned')
              WHERE run_id = %(run)s) p
        FULL JOIN (SELECT entity, sum(row_count) n, count(*) chunks, max(chunk_no) max_no
                   FROM {meta}.generation_chunks WHERE run_id = %(run)s GROUP BY 1) c ON c.entity = p.key
        WHERE p.key IS NULL OR c.entity IS NULL OR c.n <> p.value::bigint OR c.max_no <> c.chunks - 1"""),
    # Table names come from the fixed load.TABLES tuple.
    Check("ledger_matches_tables", "ledger table_rows totals (every run in the schema set) equal actual table row "
          "counts", " UNION ALL ".join(f"""
        SELECT '{t}: ledger ' || coalesce(l.n, 0) || ', table ' || c.n AS id
        FROM (SELECT count(*) n FROM {{app}}.{t}) c,
             (SELECT sum((table_rows->>'{t}')::bigint) n FROM {{meta}}.generation_chunks) l
        WHERE coalesce(l.n, 0) <> c.n""" for t in load.TABLES)),
)

DATA_CHECKS = (
    Check("order_header_matches_items", "orders.subtotal/discount/tax = sums over its items; every order has items", """
        SELECT o.order_id AS id FROM {app}.orders o
        LEFT JOIN (SELECT order_id, sum(quantity * unit_price) s, sum(discount) d, sum(tax) t
                   FROM {app}.order_items GROUP BY 1) i USING (order_id)
        WHERE i.order_id IS NULL OR (o.subtotal, o.discount, o.tax) IS DISTINCT FROM (i.s, i.d, i.t)"""),
    Check("item_lines_contiguous", "order line numbers are 1..n", """
        SELECT order_id AS id FROM {app}.order_items GROUP BY order_id HAVING max(line_no) <> count(*)"""),
    Check("item_currency_matches_order", "each item's product is priced in the order currency", """
        SELECT i.order_item_id AS id FROM {app}.order_items i
        JOIN {app}.products p USING (product_id) JOIN {app}.orders o USING (order_id)
        WHERE p.currency <> o.currency"""),
    Check("payment_amount_matches_total", "every payment attempt is for the order total (no partial payments)", """
        SELECT p.payment_id AS id FROM {app}.payment_attempts p JOIN {app}.orders o USING (order_id)
        WHERE p.amount <> o.total"""),
    Check("one_live_payment", "at most one non-FAILED attempt per order, and it is the last one", """
        SELECT order_id AS id FROM {app}.payment_attempts GROUP BY order_id
        HAVING count(*) FILTER (WHERE state <> 'FAILED') > 1
            OR max(attempt_no) FILTER (WHERE state <> 'FAILED') < max(attempt_no)"""),
    Check("payment_attempts_contiguous", "attempt numbers are 1..n", """
        SELECT order_id AS id FROM {app}.payment_attempts GROUP BY order_id HAVING max(attempt_no) <> count(*)"""),
    # `live` = state of the latest non-FAILED attempt (NULL if none).
    Check("order_status_matches_payment", "order status is consistent with its live payment attempt", """
        SELECT o.order_id AS id FROM {app}.orders o
        LEFT JOIN (SELECT order_id,
                          (array_agg(state ORDER BY attempt_no DESC) FILTER (WHERE state <> 'FAILED'))[1] live
                   FROM {app}.payment_attempts GROUP BY 1) p USING (order_id)
        WHERE NOT coalesce(CASE o.status  -- NULL counts as a violation
            WHEN 'CREATED' THEN p.live IS NULL OR p.live = 'INITIATED'
            WHEN 'CONFIRMED' THEN p.live IS NOT DISTINCT FROM 'AUTHORIZED'
            WHEN 'PROCESSING' THEN p.live IS NOT DISTINCT FROM 'AUTHORIZED'
            WHEN 'PARTIALLY_SHIPPED' THEN coalesce(p.live IN ('CAPTURED', 'SETTLED'), false)
            WHEN 'SHIPPED' THEN coalesce(p.live IN ('CAPTURED', 'SETTLED'), false)
            WHEN 'DELIVERED' THEN coalesce(p.live IN ('CAPTURED', 'SETTLED'), false)
            -- docs/03: cancellation from eligible earlier states, incl. CREATED before any payment attempt;
            -- an authorization must have been VOIDED, and no attempt may still be pending
            WHEN 'CANCELLED' THEN p.live IS NULL OR p.live = 'VOIDED'
            ELSE false END, false)"""),
    Check("timestamps_ordered", "customer/product exist before the order; payments start after it is placed", """
        SELECT o.order_id AS id FROM {app}.orders o JOIN {app}.customers c USING (customer_id)
        WHERE c.created_at > o.placed_at
        UNION SELECT i.order_item_id FROM {app}.order_items i JOIN {app}.orders o USING (order_id)
              JOIN {app}.products p USING (product_id) WHERE p.created_at > o.placed_at
        UNION SELECT p.product_id FROM {app}.products p JOIN {app}.sellers s USING (seller_id)
              WHERE s.created_at > p.created_at
        UNION SELECT p.payment_id FROM {app}.payment_attempts p JOIN {app}.orders o USING (order_id)
              WHERE p.created_at < o.placed_at"""),
    Check("category_tree_acyclic", "no category is its own ancestor", """
        WITH RECURSIVE walk (start, cur, depth) AS (
            SELECT category_id, parent_category_id, 1 FROM {app}.categories WHERE parent_category_id IS NOT NULL
            UNION ALL
            SELECT w.start, c.parent_category_id, w.depth + 1 FROM walk w
            JOIN {app}.categories c ON c.category_id = w.cur
            WHERE c.parent_category_id IS NOT NULL AND w.cur <> w.start
              AND w.depth <= (SELECT count(*) FROM {app}.categories))
        SELECT DISTINCT start AS id FROM walk WHERE cur = start"""),
)
CHECKS = LEDGER_CHECKS + DATA_CHECKS

# Python-side checks on the files themselves; the query field is unused.
FILES_INTACT = Check("files_match_ledger", "every recorded file exists with its recorded size, checksum and records",
                     "")
MANIFEST_MATCHES = Check("manifest_matches_ledger", "the run manifest lists exactly the recorded feed files", "")
# Queries over pg_temp.feed_settlements / pg_temp.feed_events, loaded from the run's files; each group runs only if
# the run published that feed. Comparisons are NULL-safe: a field missing from a row is a violation.
TEMP_TABLES = {
    "feed_settlements": "settlement_id text, payment_id text, order_id text, currency text, gross numeric,"
                        " fee numeric, net numeric, settled_at timestamptz, run_id text",
    "feed_events": "event_id text, event_type text, customer_id text, product_id text, order_id text,"
                   " event_time timestamptz, run_id text",
    "feed_windows": "run_id text, clock_start timestamptz, window_start timestamptz, window_end timestamptz",
}
# Every record lies in the window of the run that published it: (previous_as_of, as_of] for a clock step (first step:
# from clock_start), [clock_start, clock_end) for a generate run. With the per-feed "exactly once" checks this pins
# each record to exactly one step.
WINDOW_CHECK = Check("records_in_their_step_window", "every settlement / event lies in the time window of the run "
                     "(clock step) that published it", """
    SELECT f.id FROM (
        SELECT settlement_id AS id, settled_at AS at, run_id FROM pg_temp.feed_settlements
        UNION ALL SELECT event_id, event_time, run_id FROM pg_temp.feed_events) f
    JOIN pg_temp.feed_windows w USING (run_id)
    WHERE f.at IS NULL OR f.at < w.clock_start OR f.at > w.window_end
       OR (w.window_start IS NOT NULL AND f.at <= w.window_start)""")
SETTLEMENT_CHECKS = (
    Check("settlements_match_payments", "each settlement row is a SETTLED payment with the same order, amount, "
          "currency and time; fee >= 0 and fee + net = gross; no field missing", """
        SELECT coalesce(s.settlement_id, '(no settlement_id) ' || coalesce(s.payment_id, '?')) AS id
        FROM pg_temp.feed_settlements s LEFT JOIN {app}.payment_attempts p USING (payment_id)
        WHERE p.payment_id IS NULL OR p.state <> 'SETTLED' OR s.settlement_id IS NULL
           OR (p.order_id, p.amount, p.currency, p.updated_at)
              IS DISTINCT FROM (s.order_id, s.gross, s.currency, s.settled_at)
           OR s.fee IS NULL OR s.net IS NULL OR s.fee < 0 OR s.fee + s.net <> s.gross"""),
    Check("settled_payments_settled_once", "every SETTLED payment has exactly one settlement row", """
        SELECT p.payment_id AS id FROM {app}.payment_attempts p
        LEFT JOIN (SELECT payment_id, count(*) n FROM pg_temp.feed_settlements GROUP BY 1) s USING (payment_id)
        WHERE p.state = 'SETTLED' AND coalesce(s.n, 0) <> 1
        UNION ALL SELECT settlement_id FROM pg_temp.feed_settlements GROUP BY 1 HAVING count(*) > 1"""),
)
CLICKSTREAM_CHECKS = (
    Check("clickstream_ids_exist", "customer, product and order IDs in events exist (an order with its customer)", """
        SELECT e.event_id AS id FROM pg_temp.feed_events e
        WHERE (e.customer_id IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM {app}.customers c WHERE c.customer_id = e.customer_id))
           OR (e.product_id IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM {app}.products p WHERE p.product_id = e.product_id))
           OR (e.order_id IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM {app}.orders o WHERE o.order_id = e.order_id AND o.customer_id = e.customer_id))"""),
    Check("orders_placed_once", "every order has exactly one order_placed event", """
        SELECT o.order_id AS id FROM {app}.orders o
        LEFT JOIN (SELECT order_id, count(*) n FROM pg_temp.feed_events WHERE event_type = 'order_placed'
                   GROUP BY 1) e USING (order_id)
        WHERE coalesce(e.n, 0) <> 1"""),
    Check("events_unique_and_complete", "event IDs are unique, every event has an ID and type (order_placed: an "
          "order); browsing events = the run's clickstream_events (a chain: those up to its latest step)", """
        SELECT event_id AS id FROM pg_temp.feed_events WHERE event_id IS NOT NULL GROUP BY 1 HAVING count(*) > 1
        UNION ALL SELECT 'malformed event ' || coalesce(event_id, '(no event_id)') FROM pg_temp.feed_events
        WHERE event_id IS NULL OR event_type IS NULL OR (event_type = 'order_placed' AND order_id IS NULL)
        UNION ALL SELECT 'browsing events: ' || count(*) || ', expected ' || %(browse)s
        FROM pg_temp.feed_events WHERE event_type <> 'order_placed' HAVING count(*) <> %(browse)s"""),
)
FEED_CHECKS = SETTLEMENT_CHECKS + CLICKSTREAM_CHECKS + (WINDOW_CHECK,)


def _run_check(conn: psycopg.Connection, cfg: Config, check: Check, run_id: str,
               extra: dict[str, object] | None = None) -> Result:
    inner = db.q(cfg, check.query)
    params: dict[str, object] = {"run": run_id, **(extra or {})}
    row = conn.execute(sql.SQL("SELECT count(*) FROM ({}) v").format(inner), params).fetchone()
    n = int(row[0]) if row else 0
    samples: list[str] = []
    if n:
        # length-then-text puts prefix_N IDs in numeric order; LIMIT keeps the sort to a top-N heap
        samples = [r[0] for r in conn.execute(sql.SQL(
            "SELECT id::text FROM ({}) v ORDER BY length(id::text), id::text LIMIT {}").format(
            inner, sql.Literal(SAMPLE)), params)]
    return Result(check, n, samples)


def validate(conn: psycopg.Connection, cfg: Config, run_id: str | None = None) -> tuple[str, list[Result]]:
    """Run every check, file checks included, in one read-only snapshot. Returns (run_id, results)."""
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise ValidateError("validate needs a connection with no open transaction (it sets its own snapshot)")
    # Temp tables can't be created in a read-only transaction, but can be filled there: create them first.
    with conn.transaction():
        for name, cols in TEMP_TABLES.items():
            conn.execute(f"DROP TABLE IF EXISTS pg_temp.{name}")
            conn.execute(f"CREATE TEMP TABLE {name} ({cols})")
    try:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            run_id = db.resolve_run(conn, cfg, run_id)
            results = [_run_check(conn, cfg, c, run_id) for c in CHECKS]
            runs, as_of = _file_runs(conn, cfg, run_id)
            recorded = runs[0][1] if runs else {}
            published = recorded.get("feeds", {}).get("enabled") or []
            if published:  # runs from before file feeds (or with feeds disabled) publish nothing to check
                if as_of is not None and recorded.get("data_version") != generate.DATA_VERSION:
                    # a chain's browse count depends on the session times of its data version; the row checks don't
                    reason = (f"the chain was written under data_version {recorded.get('data_version')}, not "
                              f"{generate.DATA_VERSION}: check its files with the release that wrote it")
                    return run_id, results + [Result(FILES_INTACT, 1, [reason])]
                unfinished = [f"{r} is {state}: a clock step's files are checked once it completes"
                              for r, _, state in runs if state != "COMPLETED"] if as_of is not None else []
                if unfinished:  # one clear reason instead of every missing file and record of that step
                    return run_id, results + [Result(FILES_INTACT, len(unfinished), _sample(unfinished))]
                try:
                    world = load.recorded_world(recorded)
                    browse = (world.clickstream_events if as_of is None else
                              feeds.browsing_events_by(world, as_of, int(recorded["chunk_rows"])))
                except (load.LoadError, KeyError, TypeError, ValueError) as e:
                    raise ValidateError(f"run {run_id}'s recorded resume fields are malformed: {e}") from None
                results += _file_results(conn, cfg, [r for r, _, _ in runs], Path(recorded["data_root"]),
                                         published, browse)
            return run_id, results
    finally:
        if conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE:
            with conn.transaction():
                for name in TEMP_TABLES:
                    conn.execute(f"DROP TABLE IF EXISTS pg_temp.{name}")


def _file_runs(conn: psycopg.Connection, cfg: Config,
               run_id: str) -> tuple[list[tuple[str, dict[str, Any], str]], datetime | None]:
    """The runs whose files are checked, with their resume_fields and state, and the latest clock time (None: a
    generate run). A generate run alone; for a clock step, every step in the schema set (the chain shares root and
    feeds)."""
    clock = "as_of" if db.has_column(conn, cfg, "generation_runs", "as_of") else "NULL::timestamptz"
    row = conn.execute(db.q(cfg, f"SELECT resume_fields, {clock}, state FROM {{meta}}.generation_runs"
                                 " WHERE run_id = %s"), (run_id,)).fetchone()
    if row is None or not isinstance(row[0], dict):
        return [], None
    if row[1] is None:
        return [(run_id, row[0], row[2])], None
    steps = conn.execute(db.q(cfg, "SELECT run_id, resume_fields, state, as_of FROM {meta}.generation_runs"
                                   " ORDER BY as_of")).fetchall()
    return [(r[0], r[1], r[2]) for r in steps], steps[-1][3]


def _sample(bad: list[str]) -> list[str]:
    return sorted(bad, key=lambda s: (len(s), s))[:SAMPLE]


def _file_results(conn: psycopg.Connection, cfg: Config, run_ids: list[str], root: Path, published: list[str],
                  browse: int) -> list[Result]:
    """Check the runs' files against their ledgers and manifests, load the intact ones into the temp tables, and
    check the published feeds against PostgreSQL. Memory: one record at a time."""
    broken: list[str] = []
    manifest_bad: list[str] = []
    for run_id in run_ids:
        _load_run_files(conn, cfg, run_id, root, broken, manifest_bad)
    _load_windows(conn, cfg, run_ids)
    results = [Result(FILES_INTACT, len(broken), _sample(broken)),
               Result(MANIFEST_MATCHES, len(manifest_bad), _sample(manifest_bad))]
    checks = (SETTLEMENT_CHECKS if "payment_settlement" in published else ()) + (
        CLICKSTREAM_CHECKS if "clickstream" in published else ()) + (WINDOW_CHECK,)
    return results + [_run_check(conn, cfg, c, run_ids[-1], {"browse": browse}) for c in checks]


def _load_windows(conn: psycopg.Connection, cfg: Config, run_ids: list[str]) -> None:
    """pg_temp.feed_windows: each run's time window, from its recorded world and clock columns."""
    clock = "previous_as_of, as_of" if db.has_column(conn, cfg, "generation_runs", "as_of") else "NULL, NULL"
    conn.execute(db.q(cfg, "INSERT INTO pg_temp.feed_windows (run_id, clock_start, window_start, window_end)"
                           " SELECT run_id, (resume_fields->'world'->>'clock_start')::timestamptz, w.previous,"
                           " coalesce(w.as_of, (resume_fields->'world'->>'clock_end')::timestamptz)"
                           f" FROM {{meta}}.generation_runs, LATERAL (SELECT {clock}) AS w (previous, as_of)"
                           " WHERE run_id = ANY(%s)"), (run_ids,))


def _load_run_files(conn: psycopg.Connection, cfg: Config, run_id: str, root: Path, broken: list[str],
                    manifest_bad: list[str]) -> None:
    """One run's file and manifest checks (appended to broken / manifest_bad); intact feed files go into the temp
    tables."""
    deleted_by = "deleted_by" if db.has_table(conn, cfg, "cleanups") else "NULL"  # before migration 0007
    ledger = conn.execute(db.q(cfg, f"SELECT rel_path, feed, format, records, bytes, sha256, {deleted_by}"
                                    " FROM {meta}.published_files WHERE run_id = %s ORDER BY rel_path"),
                          (run_id,)).fetchall()
    intact = []
    for rel, feed, fmt, records, size, sha, cleaned_by in ledger:
        path = root / rel
        if cleaned_by:  # removed on purpose: the run's files can no longer be checked against the database
            broken.append(f"{rel}: deleted by cleanup {cleaned_by}")
            continue
        try:
            if not path.is_file():
                broken.append(f"{rel}: missing")
            elif path.stat().st_size != size or feeds.sha256_of(path) != sha:
                broken.append(f"{rel}: size or checksum differs from the ledger")
            else:
                intact.append((rel, feed, fmt, records, path))
        except OSError as e:
            broken.append(f"{rel}: unreadable ({type(e).__name__})")
    mrel = feeds.manifest_path(run_id)
    expected = {(r[0], r[5]) for r in ledger if r[1] != "manifest"}
    try:
        doc = json.loads((root / mrel).read_text(encoding="utf-8"))
        listed = {(f["file_path"], f["sha256"]) for f in doc["files"]}
        manifest_bad += sorted(f"{p} ({'unlisted' if (p, s) in expected else 'not in ledger'})"
                               for p, s in listed ^ expected)
        if doc.get("run_id") != run_id:
            manifest_bad.append(f"manifest run_id {doc.get('run_id')!r}")
    except (OSError, ValueError, KeyError, TypeError) as e:
        manifest_bad.append(f"{mrel}: unreadable ({type(e).__name__})")
    for rel, feed, fmt, records, path in intact:
        if feed == "manifest":
            continue
        n = 0
        target = "feed_settlements" if feed == "payment_settlement" else "feed_events"
        try:  # a savepoint per file: a bad record aborts only this file's COPY
            with conn.transaction(), conn.cursor() as cur, \
                    cur.copy(sql.SQL("COPY pg_temp.{} FROM STDIN").format(sql.Identifier(target))) as copy:
                for r in feeds.iter_records(path, fmt):
                    n += 1
                    if feed == "payment_settlement":
                        copy.write_row(tuple(r[k] for k in ("settlement_id", "payment_id", "order_id", "currency",
                                                            "gross_amount", "fee_amount", "net_amount",
                                                            "settled_at")) + (run_id,))
                    else:
                        payload = r.get("payload")
                        order_id = payload.get("order_id") if isinstance(payload, dict) else None
                        copy.write_row((r["event_id"], r["event_type"], r["customer_id"], r["product_id"],
                                        order_id, r.get("event_time"), run_id))
        except (ValueError, KeyError, TypeError, AttributeError, csv.Error, psycopg.DataError) as e:
            broken.append(f"{rel}: unparseable record {n} ({type(e).__name__})")
            continue
        except OSError as e:
            broken.append(f"{rel}: unreadable ({type(e).__name__})")
            continue
        if n != records:
            broken.append(f"{rel}: {n} records, ledger says {records}")
