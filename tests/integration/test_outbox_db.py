"""The transactional outbox against a real PostgreSQL: events commit with their rows or not at all, the table refuses
invalid events and edits, steps and resumes never duplicate, and older runs / schemas keep working."""

import json
import os
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from ecomgen import config, db, generate, load, validate

ADMIN_URL = os.environ.get("ECOMGEN_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="ECOMGEN_TEST_DATABASE_URL not set")

Migrated = Callable[..., psycopg.Connection]
Digest = Callable[[psycopg.Connection], dict[str, str]]
START, END = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)


@pytest.fixture
def small_cfg(cfg: config.Config) -> config.Config:
    return replace(cfg, generator=replace(cfg.generator, chunk_rows=300),
                   filesystem=replace(cfg.filesystem, csv=False, jsonl=False))


def count(c: psycopg.Connection, query: str) -> int:
    row = c.execute(query.encode()).fetchone()
    assert row is not None
    return int(row[0])


def failing(c: psycopg.Connection, cfg: config.Config) -> set[str]:
    return {r.check.name for r in validate.validate(c, cfg)[1] if not r.ok}


def test_a_failed_chunk_commits_neither_rows_nor_events_and_resume_adds_them_once(
        migrated: Migrated, small_cfg: config.Config, monkeypatch: pytest.MonkeyPatch, digest: Digest) -> None:
    """The outbox write fails after the chunk's orders were written: the whole chunk rolls back (no business change
    without its events); resume then writes it exactly once."""
    reference = migrated(small_cfg)
    load.run(reference, small_cfg, "smoke")
    c = migrated(small_cfg)
    real, calls = load._write, [0]

    def failing_outbox(cur: psycopg.Cursor[tuple[object, ...]], cfg: config.Config, table: str,
                       rows: list[tuple[object, ...]]) -> None:
        if table == load.OUTBOX:
            calls[0] += 1
            if calls[0] == 2:  # the second orders chunk: its orders, items and payments are already written
                raise RuntimeError("disk full")
        real(cur, cfg, table, rows)
    monkeypatch.setattr(load, "_write", failing_outbox)
    with pytest.raises(RuntimeError, match="disk full"):
        load.run(c, small_cfg, "smoke")
    assert count(c, "SELECT count(*) FROM commerce.orders") == 300  # chunk 0 only
    assert count(c, "SELECT count(*) FROM integration.outbox_events WHERE order_id NOT IN"
                    " (SELECT order_id FROM commerce.orders)") == 0
    assert count(c, "SELECT count(*) FROM integration.outbox_events") == count(
        c, "SELECT (table_rows->>'outbox_events')::int FROM generator_meta.generation_chunks"
           " WHERE entity = 'orders' AND chunk_no = 0")
    monkeypatch.setattr(load, "_write", real)
    assert load.resume(c, small_cfg) is not None and load.resume(c, small_cfg) is None
    assert digest(c) == digest(reference) and failing(c, small_cfg) == set()


@pytest.mark.parametrize("change, error", [
    # an order may not jump CREATED -> SHIPPED (not a transition of the state machine)
    ("payload = jsonb_set(payload, '{from}', '\"CREATED\"') WHERE event_type = 'OrderStatusChanged'"
     " AND payload->>'to' = 'SHIPPED'", psycopg.errors.CheckViolation),
    ("event_id = event_id || 'x' WHERE event_id = 'obx_ord_1_1'", psycopg.errors.CheckViolation),
    ("payment_id = NULL WHERE aggregate_type = 'payment'", psycopg.errors.CheckViolation),
    ("event_type = 'OrderStatusChanged' WHERE event_id = 'obx_ord_1_1'", psycopg.errors.CheckViolation),
    ("order_id = 'ord_999999' WHERE event_id = 'obx_ord_1_1'", psycopg.errors.CheckViolation),
    # a missing key must not slip through as NULL, and the payload must be an object
    ("payload = payload - 'from' WHERE event_type = 'OrderStatusChanged'", psycopg.errors.CheckViolation),
    ("payload = payload - 'to' WHERE event_id = 'obx_ord_1_1'", psycopg.errors.CheckViolation),
    ("payload = '[]'::jsonb WHERE event_id = 'obx_ord_1_1'", psycopg.errors.CheckViolation),
])
def test_the_table_refuses_invalid_events(migrated: Migrated, small_cfg: config.Config, change: str,
                                         error: type[Exception]) -> None:
    c = migrated(small_cfg)
    load.run(c, small_cfg, "smoke")
    c.execute("SET session_replication_role = replica")  # past the append-only trigger: the CHECKs must refuse
    with pytest.raises(error):
        c.execute(f"UPDATE integration.outbox_events SET {change}".encode())


def test_a_payment_event_belongs_to_its_payments_order(migrated: Migrated, small_cfg: config.Config) -> None:
    """Composite FK (payment_id, order_id): an otherwise valid event naming pay_1 with another order is refused."""
    c = migrated(small_cfg)
    load.run(c, small_cfg, "smoke")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        c.execute("INSERT INTO integration.outbox_events SELECT 'obx_pay_1_9', aggregate_type, aggregate_id, 9,"
                  " 'PaymentStateChanged', schema_version, occurred_at, 'ord_2', payment_id,"
                  " '{\"from\": \"CAPTURED\", \"to\": \"SETTLED\"}', run_id"
                  " FROM integration.outbox_events WHERE event_id = 'obx_pay_1_1'")


def test_a_second_schema_set_may_not_share_the_integration_schema(migrated: Migrated,
                                                                  small_cfg: config.Config) -> None:
    c = migrated(small_cfg)
    other = replace(small_cfg, postgres=replace(small_cfg.postgres, application_schema="commerce_b",
                                                metadata_schema="generator_meta_b"))
    with pytest.raises(db.MigrationError, match="already holds outbox_events"):
        db.migrate(c, other)


def test_the_outbox_is_append_only(migrated: Migrated, small_cfg: config.Config) -> None:
    c = migrated(small_cfg)
    load.run(c, small_cfg, "smoke")
    n = count(c, "SELECT count(*) FROM integration.outbox_events")
    for statement in ("UPDATE integration.outbox_events SET schema_version = 2",
                      "DELETE FROM integration.outbox_events WHERE event_id = 'obx_ord_1_1'",
                      "TRUNCATE integration.outbox_events CASCADE"):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            c.execute(statement.encode())
    with pytest.raises(psycopg.errors.ForeignKeyViolation):  # nor can the rows an event describes disappear
        c.execute("DELETE FROM commerce.payment_attempts WHERE payment_id = 'pay_1'")
    with pytest.raises(psycopg.errors.UniqueViolation):  # a replayed event is refused, not duplicated
        c.execute("INSERT INTO integration.outbox_events SELECT * FROM integration.outbox_events"
                  " WHERE event_id = 'obx_ord_1_1'")
    assert count(c, "SELECT count(*) FROM integration.outbox_events") == n


def test_steps_write_each_transition_once_with_their_lineage(migrated: Migrated, small_cfg: config.Config,
                                                            digest: Digest) -> None:
    """A chain's outbox equals a one-shot run's (run_id aside); each event was written by the step whose window
    holds it; a step spanning a whole order life records every transition while its row changed once; a repeated
    step adds nothing."""
    one_shot = migrated(small_cfg)
    load.run(one_shot, small_cfg)
    c = migrated(small_cfg)
    stops = [START + timedelta(days=d) for d in (1, 9, 20)] + [END]
    steps = [load.advance(c, small_cfg, t) for t in stops]
    assert load.advance(c, small_cfg, stops[1]) is None  # repeat: no-op
    assert digest(c) == digest(one_shot) and failing(c, small_cfg) == set()
    runs = {s.run_id: s for s in steps if s is not None}
    for run_id, previous, as_of, n in c.execute(
            "SELECT r.run_id, r.previous_as_of, r.as_of, count(e.event_id) FROM generator_meta.generation_runs r"
            " LEFT JOIN integration.outbox_events e USING (run_id) GROUP BY 1, 2, 3").fetchall():
        assert n == runs[run_id].counts[load.OUTBOX] > 0
        assert count(c, "SELECT count(*) FROM integration.outbox_events WHERE run_id = '%s' AND"
                        " (occurred_at < '%s' OR occurred_at >= '%s')"
                        % (run_id, previous or START, as_of)) == 0
    # an order placed in step 2 and delivered in it: one inserted row, five events in that step
    second = next(s for s in steps if s is not None and s.previous_as_of == stops[0])
    rows = c.execute("SELECT o.order_id, count(*) FROM commerce.orders o JOIN integration.outbox_events e"
                     " ON e.aggregate_id = o.order_id WHERE o.status = 'DELIVERED' AND e.run_id = %s"
                     " GROUP BY 1 HAVING count(*) = 5 LIMIT 1", (second.run_id,)).fetchall()
    assert rows, "no order lived its whole life inside step 2"
    events = c.execute("SELECT payload->>'to' FROM integration.outbox_events WHERE aggregate_id = %s"
                       " ORDER BY aggregate_seq", (rows[0][0],)).fetchall()
    assert [e[0] for e in events] == ["CREATED", "CONFIRMED", "PROCESSING", "SHIPPED", "DELIVERED"]


def test_older_runs_and_schemas_keep_working(migrated: Migrated, small_cfg: config.Config) -> None:
    """A run recorded under data version 4 wrote no events: the outbox checks are skipped for it. A schema
    without migration 0009 still validates, but nothing new is written into it."""
    c = migrated(small_cfg)
    load.run(c, small_cfg, "smoke")
    c.execute("UPDATE generator_meta.generation_runs SET resume_fields = resume_fields || '{\"data_version\": 4}'")
    names = {r.check.name for r in validate.validate(c, small_cfg)[1]}
    assert not names & {ch.name for ch in validate.OUTBOX_CHECKS} and failing(c, small_cfg) == set()
    c.execute("UPDATE generator_meta.generation_runs SET state = 'RUNNING', finished_at = NULL")
    with pytest.raises(load.LoadError, match="data_version differ"):  # v5 would add events a v4 run never had
        load.resume(c, small_cfg)
    old = migrated(small_cfg)
    old.execute("DROP TABLE integration.outbox_events")  # as before migration 0009
    with pytest.raises(load.LoadError, match="migration 0009"):
        load.run(old, small_cfg, "smoke")
    with pytest.raises(load.LoadError, match="migration 0009"):
        load.advance(old, small_cfg, START + timedelta(days=1))


def test_payloads_match_the_rows(migrated: Migrated, small_cfg: config.Config) -> None:
    c = migrated(small_cfg)
    load.run(c, small_cfg, "smoke")
    order = c.execute("SELECT payload FROM integration.outbox_events WHERE event_id = 'obx_ord_1_1'").fetchone()
    total = c.execute("SELECT total::text, customer_id FROM commerce.orders WHERE order_id = 'ord_1'").fetchone()
    assert order is not None and total is not None
    p = order[0] if isinstance(order[0], dict) else json.loads(order[0])
    assert (p["total"], p["customer_id"], p["to"], p["from"]) == (total[0], total[1], "CREATED", None)
    assert len(p["items"]) == count(c, "SELECT count(*) FROM commerce.order_items WHERE order_id = 'ord_1'")
    assert count(c, "SELECT count(DISTINCT schema_version) FROM integration.outbox_events") == 1
    assert generate.OUTBOX_SCHEMA_VERSION == count(c, "SELECT max(schema_version) FROM integration.outbox_events")
