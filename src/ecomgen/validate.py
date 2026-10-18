"""Cross-row invariants the schema can't enforce, plus run-ledger completeness.

Each check is a query returning one `id` per violation; only the count and a few sample IDs come back.
Scope: the application schema holds exactly one run's data (generate refuses a second run), so data checks
cover every row there; only the ledger checks are scoped to the run. Revisit when resume/increments add runs.

File checks (runs that publish feeds): every recorded file is intact and listed in the manifest, and the feeds
agree with PostgreSQL both ways (the Phase 2 gate). Feed rows are streamed into temporary tables, checked with
the same id-per-violation queries, and rolled back.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql

from ecomgen import db, feeds, load
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
    Check("ledger_matches_tables", "ledger table_rows totals equal actual table row counts", " UNION ALL ".join(f"""
        SELECT '{t}: ledger ' || coalesce(l.n, 0) || ', table ' || c.n AS id
        FROM (SELECT count(*) n FROM {{app}}.{t}) c,
             (SELECT sum((table_rows->>'{t}')::bigint) n FROM {{meta}}.generation_chunks
              WHERE run_id = %(run)s) l
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
# Queries over pg_temp.feed_settlements / pg_temp.feed_events, loaded from the run's files.
FEED_CHECKS = (
    Check("settlements_match_payments", "each settlement row is a SETTLED payment with the same order, amount, "
          "currency and time; fee >= 0 and fee + net = gross", """
        SELECT s.settlement_id AS id FROM pg_temp.feed_settlements s
        LEFT JOIN {app}.payment_attempts p USING (payment_id)
        WHERE p.payment_id IS NULL OR p.state <> 'SETTLED' OR p.order_id <> s.order_id OR p.amount <> s.gross
           OR p.currency <> s.currency OR p.updated_at <> s.settled_at OR s.fee < 0 OR s.fee + s.net <> s.gross"""),
    Check("settled_payments_settled_once", "every SETTLED payment has exactly one settlement row", """
        SELECT p.payment_id AS id FROM {app}.payment_attempts p
        LEFT JOIN (SELECT payment_id, count(*) n FROM pg_temp.feed_settlements GROUP BY 1) s USING (payment_id)
        WHERE p.state = 'SETTLED' AND coalesce(s.n, 0) <> 1
        UNION ALL SELECT settlement_id FROM pg_temp.feed_settlements GROUP BY 1 HAVING count(*) > 1"""),
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
    Check("events_unique_and_complete", "event IDs are unique; browsing events = the run's clickstream_events", """
        SELECT event_id AS id FROM pg_temp.feed_events GROUP BY 1 HAVING count(*) > 1
        UNION ALL SELECT 'browsing events: ' || count(*) || ', expected ' || %(browse)s
        FROM pg_temp.feed_events WHERE event_type <> 'order_placed' HAVING count(*) <> %(browse)s"""),
)


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
    """Run every check in one read-only snapshot. Returns (run_id, results)."""
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise ValidateError("validate needs a connection with no open transaction (it sets its own snapshot)")
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        run_id = db.resolve_run(conn, cfg, run_id)
        results = [_run_check(conn, cfg, c, run_id) for c in CHECKS]
        recorded = conn.execute(db.q(cfg, "SELECT resume_fields FROM {meta}.generation_runs WHERE run_id = %s"),
                                (run_id,)).fetchone()
    published = (recorded[0] or {}).get("feeds", {}).get("enabled") if recorded else None
    if published:  # runs from before file feeds (or with feeds disabled) publish nothing to check
        results += _file_results(conn, cfg, run_id, Path(recorded[0]["data_root"]),  # type: ignore[index]
                                 int(recorded[0]["world"]["clickstream_events"]))  # type: ignore[index]
    return run_id, results


def _sample(bad: list[str]) -> list[str]:
    return sorted(bad, key=lambda s: (len(s), s))[:SAMPLE]


def _file_results(conn: psycopg.Connection, cfg: Config, run_id: str, root: Path, browse: int) -> list[Result]:
    """Check the run's files against the ledger and manifest, then load them into temp tables (rolled back) and
    check them against PostgreSQL. Memory: one record at a time."""
    with conn.transaction(force_rollback=True):
        ledger = conn.execute(db.q(cfg, "SELECT rel_path, feed, format, records, bytes, sha256"
                                        " FROM {meta}.published_files WHERE run_id = %s ORDER BY rel_path"),
                              (run_id,)).fetchall()
        broken: list[str] = []
        intact = []
        for rel, feed, fmt, records, size, sha in ledger:
            path = root / rel
            if not path.is_file():
                broken.append(f"{rel}: missing")
            elif path.stat().st_size != size or feeds.sha256_of(path) != sha:
                broken.append(f"{rel}: size or checksum differs from the ledger")
            else:
                intact.append((rel, feed, fmt, records, path))
        mrel = feeds.manifest_path(run_id)
        expected = {(r[0], r[5]) for r in ledger if r[1] != "manifest"}
        try:
            doc = json.loads((root / mrel).read_text(encoding="utf-8"))
            listed = {(f["file_path"], f["sha256"]) for f in doc["files"]}
            manifest_bad = sorted(f"{p} ({'unlisted' if (p, s) in expected else 'not in ledger'})"
                                  for p, s in listed ^ expected)
            if doc.get("run_id") != run_id:
                manifest_bad.append(f"manifest run_id {doc.get('run_id')!r}")
        except (OSError, ValueError, KeyError, TypeError) as e:
            manifest_bad = [f"{mrel}: unreadable ({type(e).__name__})"]
        conn.execute("CREATE TEMP TABLE feed_settlements (settlement_id text, payment_id text, order_id text,"
                     " currency text, gross numeric, fee numeric, net numeric, settled_at timestamptz)")
        conn.execute("CREATE TEMP TABLE feed_events (event_id text, event_type text, customer_id text,"
                     " product_id text, order_id text)")
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
                            copy.write_row((r["settlement_id"], r["payment_id"], r["order_id"], r["currency"],
                                            r["gross_amount"], r["fee_amount"], r["net_amount"], r["settled_at"]))
                        else:
                            payload = r.get("payload")
                            order_id = payload.get("order_id") if isinstance(payload, dict) else None
                            copy.write_row((r["event_id"], r["event_type"], r["customer_id"], r["product_id"],
                                            order_id))
            except (ValueError, KeyError, TypeError, psycopg.DataError) as e:
                broken.append(f"{rel}: unparseable record {n} ({type(e).__name__})")
                continue
            if n != records:
                broken.append(f"{rel}: {n} records, ledger says {records}")
        results = [Result(FILES_INTACT, len(broken), _sample(broken)),
                   Result(MANIFEST_MATCHES, len(manifest_bad), _sample(manifest_bad))]
        return results + [_run_check(conn, cfg, c, run_id, {"browse": browse}) for c in FEED_CHECKS]
