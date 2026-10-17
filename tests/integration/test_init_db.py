"""Needs a real PostgreSQL: set ECOMGEN_TEST_DATABASE_URL (see README). Each test gets a throwaway database."""

import os
import uuid
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest
from psycopg import errors, sql

from ecomgen import config, db
from ecomgen.cli import main

ADMIN_URL = os.environ.get("ECOMGEN_TEST_DATABASE_URL", "")
EXAMPLE = Path(__file__).parents[2] / "config" / "generator.example.yaml"
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="ECOMGEN_TEST_DATABASE_URL not set")


@pytest.fixture
def db_url() -> Iterator[str]:
    name = f"ecomgen_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield urlsplit(ADMIN_URL)._replace(path=f"/{name}").geturl()
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def cfg() -> config.Config:
    return config.load(EXAMPLE)


@pytest.fixture
def conn(db_url: str, cfg: config.Config) -> Iterator[psycopg.Connection]:
    with psycopg.connect(db_url) as c:
        db.migrate(c, cfg)
        yield c


def tables(conn: psycopg.Connection, schema: str) -> set[str]:
    rows = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,))
    return {r[0] for r in rows}


def test_migrate_creates_schema_and_is_idempotent(db_url: str, cfg: config.Config) -> None:
    with psycopg.connect(db_url) as c:
        assert db.migrate(c, cfg) == ["0001_core_schema.sql"]
        assert db.migrate(c, cfg) == []
        assert tables(c, "commerce") == {
            "sellers", "categories", "customers", "products", "orders", "order_items", "payment_attempts"
        }
        assert tables(c, "generator_meta") == {"schema_migrations", "generation_runs", "generation_chunks"}
        assert c.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'integration'").fetchone()


def test_custom_schema_names(db_url: str, cfg: config.Config) -> None:
    custom = replace(cfg, postgres=replace(
        cfg.postgres, application_schema="shop", integration_schema="feeds", metadata_schema="gen"
    ))
    with psycopg.connect(db_url) as c:
        db.migrate(c, custom)
        assert "orders" in tables(c, "shop")
        assert "generation_runs" in tables(c, "gen")
        assert tables(c, "commerce") == set()


def test_changed_migration_is_refused(conn: psycopg.Connection, cfg: config.Config) -> None:
    conn.execute("UPDATE generator_meta.schema_migrations SET checksum = repeat('0', 64) WHERE version = 1")
    conn.commit()
    with pytest.raises(db.MigrationError, match="changed after it was applied"):
        db.migrate(conn, cfg)


def test_unknown_applied_migration_is_refused(conn: psycopg.Connection, cfg: config.Config) -> None:
    conn.execute("INSERT INTO generator_meta.schema_migrations VALUES (99, 'future.sql', repeat('0', 64))")
    conn.commit()
    with pytest.raises(db.MigrationError, match=r"doesn't know: \[99\]"):
        db.migrate(conn, cfg)


SEED = """
INSERT INTO commerce.sellers VALUES ('sel_1', 'S', 'EU', 'ACTIVE', '2026-01-01Z', '2026-01-01Z');
INSERT INTO commerce.categories VALUES ('cat_1', NULL, 'Books');
INSERT INTO commerce.customers VALUES
    ('cus_1', 'Ada Example', 'ada@example.com', 'DE', 'consumer', 'ACTIVE', '2026-01-01Z', '2026-01-01Z');
INSERT INTO commerce.products VALUES
    ('prd_1', 'sel_1', 'cat_1', 'Book', 10.00, 'EUR', true, '2026-01-01Z', '2026-01-01Z');
INSERT INTO commerce.orders VALUES
    ('ord_1', 'cus_1', '2026-01-02Z', 'CREATED', 'EUR', 20.00, 2.00, 3.42, 4.99, 26.41, '2026-01-02Z');
INSERT INTO commerce.order_items VALUES ('itm_1', 'ord_1', 1, 'prd_1', 2, 10.00, 2.00, 3.42, 21.42);
INSERT INTO commerce.payment_attempts VALUES
    ('pay_1', 'ord_1', 1, 'stripe-sim', 'CARD', 'CAPTURED', 26.41, 'EUR', '2026-01-02Z', '2026-01-02Z');
"""


def test_valid_rows_accepted(conn: psycopg.Connection) -> None:
    conn.execute(SEED.encode())
    assert conn.execute("SELECT total FROM commerce.orders").fetchone() == (Decimal("26.41"),)


@pytest.mark.parametrize(
    ("stmt", "error"),
    [
        # money
        ("UPDATE commerce.orders SET total = 26.40", errors.CheckViolation),
        ("UPDATE commerce.orders SET discount = 25.00", errors.CheckViolation),
        ("UPDATE commerce.order_items SET line_total = 21.41", errors.CheckViolation),
        ("UPDATE commerce.order_items SET discount = 20.01", errors.CheckViolation),
        ("UPDATE commerce.products SET price = 0", errors.CheckViolation),
        ("UPDATE commerce.payment_attempts SET amount = -1", errors.CheckViolation),
        # referential integrity
        ("UPDATE commerce.order_items SET product_id = 'prd_999'", errors.ForeignKeyViolation),
        ("DELETE FROM commerce.customers", errors.ForeignKeyViolation),
        ("UPDATE commerce.categories SET parent_category_id = 'cat_1'", errors.CheckViolation),
        # duplicates / identity
        ("INSERT INTO commerce.payment_attempts VALUES ('pay_2', 'ord_1', 1, 'p', 'CARD', 'FAILED',"
         " 1, 'EUR', '2026-01-02Z', '2026-01-02Z')", errors.UniqueViolation),
        ("UPDATE commerce.orders SET order_id = 'order-1'", errors.CheckViolation),
        # lifecycle values, PII, time
        ("UPDATE commerce.orders SET status = 'LOST'", errors.CheckViolation),
        ("UPDATE commerce.payment_attempts SET method = 'CASH'", errors.CheckViolation),
        ("UPDATE commerce.customers SET email = 'ada@gmail.com'", errors.CheckViolation),
        ("UPDATE commerce.orders SET updated_at = '2026-01-01Z'", errors.CheckViolation),
        ("UPDATE commerce.orders SET currency = 'eur'", errors.CheckViolation),
    ],
)
def test_constraints_reject_bad_rows(conn: psycopg.Connection, stmt: str, error: type[Exception]) -> None:
    conn.execute(SEED.encode())
    with pytest.raises(error):
        conn.execute(stmt.encode())


def test_run_ledger_state_consistency(conn: psycopg.Connection) -> None:
    ins = ("INSERT INTO generator_meta.generation_runs (run_id, generator_version, seed, config_sha256, profile, state,"
           " finished_at) VALUES ('r1', '0.1.0', 1, repeat('a', 64), 'smoke', %s, %s)")
    with pytest.raises(errors.CheckViolation):
        conn.execute(ins, ("COMPLETED", None))
    conn.rollback()
    with pytest.raises(errors.CheckViolation):
        conn.execute(ins, ("RUNNING", "2099-01-01Z"))


def test_cli_init_db(db_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("ECOMMERCE_DATABASE_URL", db_url)
    assert main(["init-db", str(EXAMPLE)]) == 0
    assert "applied: 0001_core_schema.sql" in capsys.readouterr().out
    assert main(["init-db", str(EXAMPLE)]) == 0
    assert "up to date" in capsys.readouterr().out


def test_cli_init_db_unreachable(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("ECOMMERCE_DATABASE_URL", "postgresql://u:topsecret@127.0.0.1:1/x?connect_timeout=2")
    assert main(["init-db", str(EXAMPLE)]) == 1
    err = capsys.readouterr().err
    assert "database error" in err and "topsecret" not in err
