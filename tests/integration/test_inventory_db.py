"""Inventory against a real PostgreSQL: movements and stock commit with their chunk or not at all, the tables refuse
invalid stock and edits, steps leave inventory as of their time, and older runs / schemas keep working."""

import os
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from ecomgen import config, generate, load, validate

ADMIN_URL = os.environ.get("ECOMGEN_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="ECOMGEN_TEST_DATABASE_URL not set")

Migrated = Callable[..., psycopg.Connection]
Digest = Callable[[psycopg.Connection], dict[str, str]]
START = datetime(2026, 1, 1, tzinfo=UTC)
SUM_DIFFERS = """SELECT count(*) FROM commerce.inventory i FULL JOIN (
    SELECT warehouse_id, product_id, sum(on_hand_delta) h, sum(reserved_delta) r FROM commerce.inventory_movements
    GROUP BY 1, 2) m USING (warehouse_id, product_id) WHERE (i.on_hand, i.reserved) IS DISTINCT FROM (m.h, m.r)"""


@pytest.fixture
def small_cfg(cfg: config.Config) -> config.Config:
    return replace(cfg, generator=replace(cfg.generator, chunk_rows=300),
                   filesystem=replace(cfg.filesystem, csv=False, jsonl=False))


def count(c: psycopg.Connection, query: str) -> int:
    row = c.execute(query.encode()).fetchone()
    assert row is not None
    return int(row[0])


def count_text(c: psycopg.Connection, query: str) -> str:
    row = c.execute(query.encode()).fetchone()
    assert row is not None
    return str(row[0])


def failing(c: psycopg.Connection, cfg: config.Config) -> set[str]:
    return {r.check.name for r in validate.validate(c, cfg)[1] if not r.ok}


def test_a_failed_chunk_rolls_back_its_movements_and_stock_and_resume_applies_them_once(
        migrated: Migrated, small_cfg: config.Config, monkeypatch: pytest.MonkeyPatch, digest: Digest) -> None:
    """The movement insert fails after the chunk's stock deltas were applied: both roll back with the chunk."""
    reference = migrated(small_cfg)
    load.run(reference, small_cfg, "smoke")
    c = migrated(small_cfg)
    real, calls = load._write, [0]

    def failing_moves(cur: psycopg.Cursor[tuple[object, ...]], cfg: config.Config, table: str,
                      rows: list[tuple[object, ...]]) -> None:
        if table == load.MOVEMENTS:
            calls[0] += 1
            if calls[0] == 3:  # orders chunk 1 (after the inventory chunk and orders chunk 0)
                raise RuntimeError("disk full")
        real(cur, cfg, table, rows)
    monkeypatch.setattr(load, "_write", failing_moves)
    with pytest.raises(RuntimeError, match="disk full"):
        load.run(c, small_cfg, "smoke")
    assert count(c, "SELECT count(*) FROM commerce.orders") == 300  # orders chunk 0 only
    assert count(c, SUM_DIFFERS) == 0 and count(c, "SELECT count(*) FROM commerce.inventory_movements"
                                                  " WHERE order_item_id IS NOT NULL") > 0
    monkeypatch.setattr(load, "_write", real)
    assert load.resume(c, small_cfg) is not None and load.resume(c, small_cfg) is None
    assert digest(c) == digest(reference) and failing(c, small_cfg) == set()


@pytest.mark.parametrize("statement", [
    "UPDATE commerce.inventory SET reserved = on_hand + 1 WHERE product_id = 'prd_1'",
    "UPDATE commerce.inventory SET reserved = -1 WHERE product_id = 'prd_1'",
    # past the append-only trigger (replica mode): the CHECK refuses deltas that don't match the type, a wrong id,
    # and an order movement without its line
    "SET session_replication_role = replica; UPDATE commerce.inventory_movements SET on_hand_delta = 0"
    " WHERE movement_type = 'SHIP'",
    "SET session_replication_role = replica; UPDATE commerce.inventory_movements SET movement_id = movement_id || 'x'"
    " WHERE movement_type = 'RESERVE'",
    "SET session_replication_role = replica; UPDATE commerce.inventory_movements SET order_item_id = NULL"
    " WHERE movement_type = 'RESERVE'",
    "SET session_replication_role = replica; UPDATE commerce.inventory_movements SET movement_type = 'BONUS'"
    " WHERE movement_type = 'RECEIPT'",
])
def test_the_tables_refuse_invalid_stock(migrated: Migrated, small_cfg: config.Config, statement: str) -> None:
    c = migrated(small_cfg)
    load.run(c, small_cfg, "smoke")
    with pytest.raises(psycopg.errors.CheckViolation):
        c.execute(statement.encode())


def test_a_movement_is_for_its_lines_product_and_is_append_only(migrated: Migrated, small_cfg: config.Config) -> None:
    c = migrated(small_cfg)
    load.run(c, small_cfg, "smoke")
    n = count(c, "SELECT count(*) FROM commerce.inventory_movements")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):  # composite FK (order_item_id, product_id)
        c.execute("INSERT INTO commerce.inventory_movements SELECT 'mov_' || m.order_item_id || '_rel', i.warehouse_id,"
                  " i.product_id, 'RELEASE', 1, 0, -1, m.occurred_at, m.order_item_id, m.run_id"
                  " FROM commerce.inventory_movements m, commerce.inventory i WHERE m.movement_type = 'RESERVE'"
                  " AND i.product_id <> m.product_id ORDER BY m.movement_id, i.product_id LIMIT 1")
    for statement in ("UPDATE commerce.inventory_movements SET quantity = quantity",
                      "DELETE FROM commerce.inventory_movements WHERE movement_type = 'SHIP'",
                      "TRUNCATE commerce.inventory_movements CASCADE"):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            c.execute(statement.encode())
    assert count(c, "SELECT count(*) FROM commerce.inventory_movements") == n


def test_each_step_leaves_inventory_as_of_its_time(migrated: Migrated, small_cfg: config.Config,
                                                  digest: Digest) -> None:
    """After each step, inventory is the one-shot run's movements before its as_of summed; together the steps equal
    the one-shot run (a stop at exactly a delivery time included), and a repeated step adds nothing."""
    one_shot = migrated(small_cfg)
    load.run(one_shot, small_cfg)
    c = migrated(small_cfg)
    stops = [START + timedelta(days=1), START + timedelta(days=1, hours=6), START + timedelta(days=10),
             small_cfg.generator.clock_end]
    for as_of in stops:
        step = load.advance(c, small_cfg, as_of)
        assert step is not None and step.counts[load.MOVEMENTS] > 0
        expected = one_shot.execute("SELECT warehouse_id, product_id, sum(on_hand_delta), sum(reserved_delta)"
                                    " FROM commerce.inventory_movements WHERE occurred_at < %s GROUP BY 1, 2"
                                    " ORDER BY 1, 2", (as_of,)).fetchall()
        assert c.execute("SELECT warehouse_id, product_id, on_hand, reserved FROM commerce.inventory"
                         " ORDER BY 1, 2").fetchall() == expected
    assert load.advance(c, small_cfg, stops[1]) is None
    assert digest(c) == digest(one_shot) and failing(c, small_cfg) == set()
    # each bound of a step's window, alone: step 3 recorded as starting an hour late (its 06:00 delivery and
    # first hour fall before it), then step 2 as ending an hour early (its last hour falls after it)
    # (replica mode: past the chain's FK previous_as_of -> previous step's as_of; test database only)
    for column, as_of in (("previous_as_of", stops[2]), ("as_of", stops[1])):
        run = count_text(c, f"SELECT run_id FROM generator_meta.generation_runs WHERE as_of = '{as_of.isoformat()}'")
        shrink = "+" if column == "previous_as_of" else "-"
        c.execute(f"SET session_replication_role = replica; UPDATE generator_meta.generation_runs"
                  f" SET {column} = {column} {shrink} interval '1 hour' WHERE run_id = '{run}'".encode())
        assert "movements_in_run_window" in failing(c, small_cfg), column
        c.execute(f"UPDATE generator_meta.generation_runs SET {column} = {column} {'-' if shrink == '+' else '+'}"
                  f" interval '1 hour' WHERE run_id = '{run}'; RESET session_replication_role".encode())
    assert failing(c, small_cfg) == set()


def test_a_schema_set_with_stray_stock_rows_is_refused(migrated: Migrated, small_cfg: config.Config) -> None:
    """Rows no run recorded (here one warehouse) make generate refuse cleanly, before any chunk."""
    c = migrated(small_cfg)
    c.execute("INSERT INTO commerce.warehouses VALUES ('wh_1', 'x', 'NA', 1, '2025-01-01Z', '2025-01-01Z')")
    with pytest.raises(load.LoadError, match="already hold rows that no run recorded"):
        load.run(c, small_cfg, "smoke")
    assert count(c, "SELECT count(*) FROM generator_meta.generation_runs") == 0


def test_the_loader_refuses_movements_for_a_cell_without_a_row(migrated: Migrated, small_cfg: config.Config) -> None:
    c = migrated(small_cfg)
    move = generate._move("mov_rpl_wh_1_prd_1_1", 1, "prd_1", "REPLENISHMENT", 5, START)
    with pytest.raises(load.LoadError, match="have no row"), c.transaction(), c.cursor() as cur:
        load._apply_stock(cur, small_cfg, [move])


def test_older_runs_and_schemas_keep_working(migrated: Migrated, small_cfg: config.Config) -> None:
    """A run recorded under data version 5 wrote no inventory: those checks are skipped and it can't be resumed with
    this version. A schema without migration 0010 still validates, but nothing new is written into it."""
    c = migrated(small_cfg)
    load.run(c, small_cfg, "smoke")
    c.execute("UPDATE generator_meta.generation_runs SET resume_fields = resume_fields || '{\"data_version\": 5}'")
    names = {r.check.name for r in validate.validate(c, small_cfg)[1]}
    assert not names & {ch.name for ch in validate.INVENTORY_CHECKS} and failing(c, small_cfg) == set()
    c.execute("UPDATE generator_meta.generation_runs SET state = 'RUNNING', finished_at = NULL")
    with pytest.raises(load.LoadError, match="data_version differ"):
        load.resume(c, small_cfg)
    old = migrated(small_cfg)
    old.execute("DROP TABLE commerce.inventory_movements, commerce.inventory, commerce.warehouses;"
                " DELETE FROM generator_meta.schema_migrations WHERE version = 10")  # as before migration 0010
    with pytest.raises(load.LoadError, match="migration 0010"):
        load.run(old, small_cfg, "smoke")
    with pytest.raises(load.LoadError, match="migration 0010"):
        load.advance(old, small_cfg, START)
