"""The practice runbook's reader grants against a real PostgreSQL: a second practice schema set, granted exactly as
docs/PRACTICE_RUNBOOK.md says, is readable by a login role that can change nothing there."""

import os
import re
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from ecomgen import config, load

ADMIN_URL = os.environ.get("ECOMGEN_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="ECOMGEN_TEST_DATABASE_URL not set")
RUNBOOK = Path(__file__).parents[2] / "docs" / "PRACTICE_RUNBOOK.md"
SCHEMAS = ("practice2_commerce", "practice2_integration", "practice2_meta")


def runbook_grants() -> list[str]:
    """The -c statements of the fenced block after the runbook's reader-grants marker."""
    text = RUNBOOK.read_text(encoding="utf-8")
    block = text[text.index("<!-- reader-grants"):].split("```")[1]
    statements = re.findall(r'-c "([^"]+)"', block)
    assert len(statements) == 3 and all(s in statements[0] for s in SCHEMAS)
    return statements


@pytest.fixture
def reader(db_url: str) -> Iterator[tuple[str, str]]:
    """A throwaway login role (role names are cluster-wide, so unique per test): (name, password)."""
    name, password = f"reader_{uuid.uuid4().hex[:10]}", uuid.uuid4().hex
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE").format(
            sql.Identifier(name), sql.Literal(password)))
        admin.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(db_url.rsplit("/", 1)[1]), sql.Identifier(name)))
    try:
        yield name, password
    finally:
        with psycopg.connect(db_url, autocommit=True) as c:  # its grants and default privileges there, then the role
            c.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                sql.Identifier(db_url.rsplit("/", 1)[1]), sql.Identifier(name)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def test_the_runbook_grants_let_the_reader_read_a_second_set_and_change_nothing(
        migrated: Callable[..., psycopg.Connection], db_url: str, cfg: config.Config,
        reader: tuple[str, str]) -> None:
    practice = replace(cfg, generator=replace(cfg.generator, chunk_rows=300),
                       postgres=replace(cfg.postgres, application_schema=SCHEMAS[0], integration_schema=SCHEMAS[1],
                                        metadata_schema=SCHEMAS[2]),
                       filesystem=replace(cfg.filesystem, csv=False, jsonl=False))
    migrated(cfg, db_url)  # a first set (commerce / integration / generator_meta) the reader is not granted
    owner = migrated(practice, db_url)
    load.run(owner, practice, "smoke")
    name, password = reader
    for statement in runbook_grants():  # as the set's owner, like the runbook
        owner.execute(statement.replace("ecomgen_reader", name).encode())

    tables = owner.execute("SELECT schemaname, tablename FROM pg_tables WHERE schemaname = ANY(%s) ORDER BY 1, 2",
                           (list(SCHEMAS),)).fetchall()
    assert len(tables) == 18  # 10 commerce, 1 integration, 7 meta
    with psycopg.connect(db_url, user=name, password=password, autocommit=True) as r:
        for schema, table in tables:
            r.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, table)))
        orders = sql.Identifier(SCHEMAS[0], "orders")
        for change in (
                sql.SQL("INSERT INTO {} SELECT * FROM {} WHERE false").format(orders, orders),
                sql.SQL("UPDATE {} SET status = status WHERE false").format(orders),
                sql.SQL("DELETE FROM {} WHERE false").format(orders),
                sql.SQL("TRUNCATE {}").format(orders),
                sql.SQL("CREATE TABLE {} (x int)").format(sql.Identifier(SCHEMAS[0], "reader_probe")),
                sql.SQL("ALTER TABLE {} ADD COLUMN x int").format(orders),
                sql.SQL("DROP TABLE {}").format(sql.Identifier(SCHEMAS[1], "outbox_events"))):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                r.execute(change)
        # a table the owner adds later (a future init-db) is readable through the default privileges
        owner.execute(sql.SQL("CREATE TABLE {} (x int)").format(sql.Identifier(SCHEMAS[0], "added_later")))
        assert r.execute(sql.SQL("SELECT count(*) FROM {}").format(
            sql.Identifier(SCHEMAS[0], "added_later"))).fetchone() == (0,)
        # and the first practice set's schemas, never granted, stay closed
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            r.execute("SELECT 1 FROM commerce.orders LIMIT 1")
