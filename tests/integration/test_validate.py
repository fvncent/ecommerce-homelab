"""`validate` against a real PostgreSQL: a clean run passes; each corruption is caught by its check."""

import os
from collections.abc import Callable
from dataclasses import replace

import psycopg
import pytest

from ecomgen import config, db, load, validate
from ecomgen.cli import main

ADMIN_URL = os.environ.get("ECOMGEN_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="ECOMGEN_TEST_DATABASE_URL not set")


@pytest.fixture
def small_cfg(cfg: config.Config) -> config.Config:
    # Feeds off: these tests break database invariants one at a time, and the feed checks (test_publish.py)
    # would rightly flag the same edits as files disagreeing with the database.
    return replace(cfg, generator=replace(cfg.generator, chunk_rows=300),
                   filesystem=replace(cfg.filesystem, csv=False, jsonl=False))


@pytest.fixture
def loaded(migrated: Callable[..., psycopg.Connection], small_cfg: config.Config) -> psycopg.Connection:
    c = migrated(small_cfg)
    load.run(c, small_cfg, "smoke")
    return c


def failing(conn: psycopg.Connection, cfg: config.Config, run_id: str | None = None) -> set[str]:
    return {r.check.name for r in validate.validate(conn, cfg, run_id)[1] if not r.ok}


def test_clean_run_passes(loaded: psycopg.Connection, small_cfg: config.Config) -> None:
    run_id, results = validate.validate(loaded, small_cfg)
    assert run_id.startswith("run_")
    assert [r.check.name for r in results if not r.ok] == []
    assert len(results) == len(validate.CHECKS) + len(validate.OUTBOX_CHECKS)


# Each statement breaks exactly one invariant the schema can't see (all CHECK/FK constraints still hold).
FIRST_SETTLED = "(SELECT order_id FROM commerce.payment_attempts WHERE state = 'SETTLED' ORDER BY 1 LIMIT 1)"
CORRUPTIONS = {
    "order_header_matches_items": "UPDATE commerce.orders SET subtotal = subtotal + 1, total = total + 1"
                                  " WHERE order_id = 'ord_1'; UPDATE commerce.payment_attempts"
                                  " SET amount = amount + 1 WHERE order_id = 'ord_1'",
    "item_lines_contiguous": "UPDATE commerce.order_items SET line_no = 9 WHERE order_item_id = 'itm_1'",
    "item_currency_matches_order": "UPDATE commerce.products SET currency = 'EUR'"
                                   " WHERE product_id = (SELECT product_id FROM commerce.order_items LIMIT 1)",
    "payment_amount_matches_total": "UPDATE commerce.payment_attempts SET amount = amount + 0.01"
                                    " WHERE payment_id = (SELECT min(payment_id) FROM commerce.payment_attempts)",
    # an earlier FAILED attempt turned live; the latest live attempt (SETTLED) still matches the order status
    "one_live_payment": "UPDATE commerce.payment_attempts SET state = 'AUTHORIZED' WHERE payment_id ="
                        " (SELECT min(f.payment_id) FROM commerce.payment_attempts f JOIN commerce.payment_attempts s"
                        " USING (order_id) WHERE f.state = 'FAILED' AND s.state = 'SETTLED')",
    "payment_attempts_contiguous": "UPDATE commerce.payment_attempts SET attempt_no = 7"
                                   f" WHERE order_id = {FIRST_SETTLED} AND state = 'SETTLED'",
    "order_status_matches_payment": f"UPDATE commerce.orders SET status = 'CREATED' WHERE order_id = {FIRST_SETTLED}",
    "timestamps_ordered": "UPDATE commerce.customers SET created_at = '2030-01-01Z', updated_at = '2030-01-01Z'"
                          " WHERE customer_id = (SELECT customer_id FROM commerce.orders WHERE order_id = 'ord_1')",
    "category_tree_acyclic": "UPDATE commerce.categories SET parent_category_id = 'cat_9' WHERE category_id = 'cat_1'",
    "ledger_matches_tables": "UPDATE generator_meta.generation_chunks SET table_rows = jsonb_set(table_rows,"
                             " '{order_items}', to_jsonb((table_rows->>'order_items')::int + 1))"
                             " WHERE entity = 'orders' AND chunk_no = 0",
    "ledger_complete": "INSERT INTO generator_meta.generation_chunks (run_id, entity, chunk_no, row_count)"
                       " SELECT run_id, 'refunds', 0, 0 FROM generator_meta.generation_runs",
    "run_completed": "UPDATE generator_meta.generation_runs SET state = 'FAILED'",
}
# A hand-edited status or amount also no longer matches the order's / payment's outbox history.
HISTORY_TOO = {"order_header_matches_items", "payment_amount_matches_total", "one_live_payment",
               "order_status_matches_payment"}
# outbox_events is append-only (trigger); replica mode skips triggers (test databases are ours, superuser), while
# its CHECK constraints still hold, so each statement breaks only what the table can't see.
REPLICA = "SET session_replication_role = replica; "
OUTBOX_CORRUPTIONS = {
    "outbox_ledger_matches_table": "UPDATE generator_meta.generation_chunks SET table_rows = jsonb_set(table_rows,"
                                   " '{outbox_events}', to_jsonb((table_rows->>'outbox_events')::int + 1))"
                                   " WHERE entity = 'orders' AND chunk_no = 0",
    "outbox_reconstructs_rows": REPLICA + "UPDATE integration.outbox_events"
                                " SET payload = jsonb_set(payload, '{total}', '\"0.00\"') WHERE event_id = 'obx_ord_1_1'",
    # the 2nd of a 3+ event chain no later than the 1st (neither the creation nor the latest event changes)
    "outbox_chains_contiguous": REPLICA + "UPDATE integration.outbox_events e SET occurred_at = f.occurred_at"
                                " FROM integration.outbox_events f WHERE e.event_id = (SELECT 'obx_' || aggregate_id"
                                " || '_2' FROM integration.outbox_events WHERE aggregate_seq = 3"
                                " AND aggregate_type = 'order' ORDER BY event_id LIMIT 1)"
                                " AND f.aggregate_id = e.aggregate_id AND f.aggregate_seq = 1",
    "outbox_events_in_run_window": "UPDATE generator_meta.generation_runs SET resume_fields = jsonb_set(resume_fields,"
                                   " '{world,clock_start}', to_jsonb((SELECT min(occurred_at) + interval '1 second'"
                                   " FROM integration.outbox_events)::text))",
}


def test_every_check_has_a_corruption_test() -> None:
    assert set(CORRUPTIONS) == {c.name for c in validate.CHECKS}
    assert set(OUTBOX_CORRUPTIONS) == {c.name for c in validate.OUTBOX_CHECKS}


@pytest.mark.parametrize("check", sorted(CORRUPTIONS))
def test_corruption_is_caught_by_exactly_its_check(
    loaded: psycopg.Connection, small_cfg: config.Config, check: str
) -> None:
    loaded.execute(CORRUPTIONS[check].encode())
    assert failing(loaded, small_cfg) == {check} | ({"outbox_reconstructs_rows"} if check in HISTORY_TOO else set())


@pytest.mark.parametrize("check", sorted(OUTBOX_CORRUPTIONS))
def test_outbox_corruption_is_caught_by_exactly_its_check(
    loaded: psycopg.Connection, small_cfg: config.Config, check: str
) -> None:
    loaded.execute(OUTBOX_CORRUPTIONS[check].encode())
    loaded.execute("RESET session_replication_role")
    assert failing(loaded, small_cfg) == {check}


# (order status, live payment state) pairs that must fail, beyond what the generator produces
INCONSISTENT = [("CONFIRMED", "SETTLED"), ("PROCESSING", "CAPTURED"), ("SHIPPED", "AUTHORIZED"),
                ("CANCELLED", "INITIATED"), ("CANCELLED", "SETTLED"), ("CREATED", "AUTHORIZED")]


@pytest.mark.parametrize(("status", "state"), INCONSISTENT)
def test_inconsistent_status_payment_pairs(
    loaded: psycopg.Connection, small_cfg: config.Config, status: str, state: str
) -> None:
    loaded.execute(f"UPDATE commerce.orders SET status = '{status}' WHERE order_id = {FIRST_SETTLED};"
                   f" UPDATE commerce.payment_attempts SET state = '{state}'"
                   f" WHERE order_id = {FIRST_SETTLED} AND state = 'SETTLED'".encode())
    assert failing(loaded, small_cfg) == {"order_status_matches_payment", "outbox_reconstructs_rows"}


def test_status_rules_accept_valid_edge_cases(loaded: psycopg.Connection, small_cfg: config.Config) -> None:
    # split shipment with captured payment
    # (the row rules accept these; the outbox rightly says the rows no longer match their recorded history)
    loaded.execute(f"UPDATE commerce.orders SET status = 'PARTIALLY_SHIPPED' WHERE order_id = {FIRST_SETTLED}")
    assert failing(loaded, small_cfg) == {"outbox_reconstructs_rows"}
    # docs/03 allows cancelling from CREATED, i.e. before any payment attempt. A payment with outbox events can't
    # be deleted (FK); replica mode skips that check here (test database only).
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        loaded.execute(f"DELETE FROM commerce.payment_attempts WHERE order_id = {FIRST_SETTLED}")
    loaded.execute(f"SET session_replication_role = replica; UPDATE commerce.orders SET status = 'CANCELLED'"
                   f" WHERE order_id = {FIRST_SETTLED}; DELETE FROM commerce.payment_attempts"
                   f" WHERE order_id = {FIRST_SETTLED}; RESET session_replication_role".encode())
    # ledger: rows were deleted behind it; outbox: the order's history no longer matches it
    assert failing(loaded, small_cfg) == {"ledger_matches_tables", "outbox_reconstructs_rows"}


def test_missing_chunk_marker_is_caught(loaded: psycopg.Connection, small_cfg: config.Config) -> None:
    loaded.execute("DELETE FROM generator_meta.generation_chunks WHERE entity = 'orders' AND chunk_no = 1")
    _, results = validate.validate(loaded, small_cfg)
    bad = {r.check.name: r for r in results if not r.ok}
    assert set(bad) == {"ledger_complete", "ledger_matches_tables", "outbox_ledger_matches_table"}
    assert bad["ledger_complete"].samples == ["orders: planned 1000, ledger 700 rows in 3 chunks (max chunk_no 3)"]


def test_samples_are_bounded(loaded: psycopg.Connection, small_cfg: config.Config) -> None:
    loaded.execute("UPDATE commerce.payment_attempts SET amount = amount + 0.01")
    r = next(r for r in validate.validate(loaded, small_cfg)[1] if r.check.name == "payment_amount_matches_total")
    n = loaded.execute("SELECT count(*) FROM commerce.payment_attempts").fetchone()
    assert n is not None and r.violations == n[0]
    assert r.samples == sorted(r.samples, key=lambda i: int(i[4:]))[:validate.SAMPLE]  # lowest IDs, numeric order
    assert len(r.samples) == validate.SAMPLE


def test_run_selection(loaded: psycopg.Connection, small_cfg: config.Config) -> None:
    latest, _ = validate.validate(loaded, small_cfg)
    assert validate.validate(loaded, small_cfg, latest)[0] == latest
    with pytest.raises(db.RunLookupError, match="not found"):
        validate.validate(loaded, small_cfg, "run_nope")


def test_refuses_open_transaction(loaded: psycopg.Connection, small_cfg: config.Config) -> None:
    loaded.autocommit = False
    loaded.execute("SELECT 1")
    with pytest.raises(validate.ValidateError, match="no open transaction"):
        validate.validate(loaded, small_cfg)
    loaded.rollback()
    assert validate.validate(loaded, small_cfg)[0].startswith("run_")  # idle non-autocommit is fine


def test_no_runs(migrated: Callable[..., psycopg.Connection], small_cfg: config.Config) -> None:
    with pytest.raises(db.RunLookupError, match="no runs recorded"):
        validate.validate(migrated(small_cfg), small_cfg)


def test_cli_validate(
    db_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ECOMMERCE_DATABASE_URL", db_url)
    cfg_path = "config/generator.example.yaml"
    assert main(["init-db", cfg_path]) == 0
    assert main(["validate", cfg_path]) == 3  # could not validate, distinct from bad data
    assert "no runs recorded" in capsys.readouterr().err
    assert main(["generate", cfg_path, "--profile", "smoke"]) == 0
    capsys.readouterr()
    assert main(["validate", cfg_path]) == 0
    passed = len(validate.CHECKS) + len(validate.OUTBOX_CHECKS) + 2 + len(validate.FEED_CHECKS)
    assert f"{passed} passed, 0 failed" in capsys.readouterr().out
    assert main(["validate", cfg_path, "--run-id", "run_nope"]) == 3
    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute("UPDATE commerce.order_items SET line_no = 9 WHERE order_item_id = 'itm_1'")
    assert main(["validate", cfg_path]) == 1
    out = capsys.readouterr().out
    assert "FAIL item_lines_contiguous: 1 violation(s)" in out and "ord_1" in out
