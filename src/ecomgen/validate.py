"""Cross-row invariants the schema can't enforce, plus run-ledger completeness.

Each check is a query returning one `id` per violation; only the count and a few sample IDs come back.
Scope: the application schema holds exactly one run's data (generate refuses a second run), so data checks
cover every row there; only the ledger checks are scoped to the run. Revisit when resume/increments add runs.
"""

from dataclasses import dataclass

import psycopg
from psycopg import sql

from ecomgen import db, load
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


def _run_check(conn: psycopg.Connection, cfg: Config, check: Check, run_id: str) -> Result:
    inner = db.q(cfg, check.query)
    params = {"run": run_id}
    row = conn.execute(sql.SQL("SELECT count(*) FROM ({}) v").format(inner), params).fetchone()
    n = int(row[0]) if row else 0
    samples: list[str] = []
    if n:
        # length-then-text puts prefix_N IDs in numeric order; LIMIT keeps the sort to a top-N heap
        samples = [r[0] for r in conn.execute(sql.SQL(
            "SELECT id::text FROM ({}) v ORDER BY length(id::text), id::text LIMIT {}").format(
            inner, sql.Literal(SAMPLE)), params)]
    return Result(check, n, samples)


def resolve_run(conn: psycopg.Connection, cfg: Config, run_id: str | None) -> str:
    if not db.has_ledger(conn, cfg):
        raise ValidateError("generator schema not found; run `ecomgen init-db` first")
    if run_id is None:
        row = conn.execute(db.q(cfg, "SELECT run_id FROM {meta}.generation_runs ORDER BY started_at DESC LIMIT 1"
                                )).fetchone()
        if row is None:
            raise ValidateError("no runs recorded; run `ecomgen generate` first")
    else:
        row = conn.execute(db.q(cfg, "SELECT run_id FROM {meta}.generation_runs WHERE run_id = %s"),
                           (run_id,)).fetchone()
        if row is None:
            raise ValidateError(f"run {run_id} not found")
    return str(row[0])


def validate(conn: psycopg.Connection, cfg: Config, run_id: str | None = None) -> tuple[str, list[Result]]:
    """Run every check in one read-only snapshot. Returns (run_id, results)."""
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise ValidateError("validate needs a connection with no open transaction (it sets its own snapshot)")
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        run_id = resolve_run(conn, cfg, run_id)
        return run_id, [_run_check(conn, cfg, c, run_id) for c in CHECKS]
