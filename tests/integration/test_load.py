"""Loader + `generate` against a real PostgreSQL. Fixtures are in conftest.py."""

import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest

from ecomgen import config, generate, load
from ecomgen.cli import main

ADMIN_URL = os.environ.get("ECOMGEN_TEST_DATABASE_URL", "")
EXAMPLE = Path(__file__).parents[2] / "config" / "generator.example.yaml"
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="ECOMGEN_TEST_DATABASE_URL not set")

SMOKE_ORDERS = load.PROFILE_COUNTS["smoke"]["orders"]


@pytest.fixture
def small_cfg(cfg: config.Config) -> config.Config:
    # 300-row chunks: smoke orders span 4 chunks, so partial-run behaviour is observable
    return replace(cfg, generator=replace(cfg.generator, chunk_rows=300))


Migrated = Callable[..., psycopg.Connection]


@pytest.fixture
def aconn(migrated: Migrated, db_url: str, small_cfg: config.Config) -> psycopg.Connection:
    return migrated(small_cfg, db_url)


def one(conn: psycopg.Connection, query: str) -> object:
    row = conn.execute(query.encode()).fetchone()
    assert row is not None
    return row[0]


def table_digest(conn: psycopg.Connection) -> dict[str, str]:
    """Content hash of every source table (rows sorted by their text form, so order is deterministic)."""
    return {t: str(one(conn, f"SELECT md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), ''))"
                             f" FROM commerce.{t} x")) for t in load.TABLES}


def reconciliation_errors(conn: psycopg.Connection) -> int:
    return int(str(one(conn, """
        SELECT (SELECT count(*) FROM commerce.orders o
                LEFT JOIN (SELECT order_id, sum(quantity * unit_price) s, sum(discount) d, sum(tax) t
                           FROM commerce.order_items GROUP BY 1) i USING (order_id)
                WHERE i.order_id IS NULL OR (o.subtotal, o.discount, o.tax) <> (i.s, i.d, i.t))
             + (SELECT count(*) FROM commerce.payment_attempts p JOIN commerce.orders o USING (order_id)
                WHERE p.amount <> o.total)
             + (SELECT count(*) FROM commerce.order_items i JOIN commerce.products p USING (product_id)
                JOIN commerce.orders o USING (order_id) WHERE p.currency <> o.currency OR NOT p.active)""")))


def test_cli_generate_smoke(
    db_url: str, cfg: config.Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ECOMMERCE_DATABASE_URL", db_url)
    assert main(["init-db", str(EXAMPLE)]) == 0
    assert main(["generate", str(EXAMPLE), "--profile", "smoke"]) == 0
    out = capsys.readouterr().out
    assert "COMPLETED" in out and f"orders={SMOKE_ORDERS}" in out
    with psycopg.connect(db_url) as c:
        assert one(c, "SELECT count(*) FROM commerce.orders") == SMOKE_ORDERS
        assert c.execute("SELECT state, profile, counts->'planned'->>'orders', counts->'written'->>'orders'"
                         " FROM generator_meta.generation_runs").fetchall() == [
            ("COMPLETED", "smoke", str(SMOKE_ORDERS), str(SMOKE_ORDERS))]
        assert reconciliation_errors(c) == 0
    # a second run would collide with the deterministic IDs: refused, nothing written
    assert main(["generate", str(EXAMPLE), "--profile", "smoke"]) == 1
    assert "already holds run" in capsys.readouterr().err


def test_chunk_ledger_matches_rows(aconn: psycopg.Connection, small_cfg: config.Config) -> None:
    result = load.run(aconn, small_cfg, "smoke")
    assert result.chunks_written == 9 and result.chunks_skipped == 0  # 1+1+2+1+4
    ledger: dict[str, int] = dict(aconn.execute("SELECT entity, sum(row_count) FROM generator_meta.generation_chunks"
                                " GROUP BY 1").fetchall())
    assert ledger == {"sellers": 20, "categories": 40, "customers": 500, "products": 200, "orders": SMOKE_ORDERS}
    actual = {t: one(aconn, f"SELECT count(*) FROM commerce.{t}") for t in load.TABLES}
    assert result.counts == actual  # child tables too, from the ledger's table_rows


def test_same_seed_same_database(migrated: Migrated, small_cfg: config.Config) -> None:
    """T02: two clean databases, same config, identical source data (and chunk size doesn't matter)."""
    big = replace(small_cfg, generator=replace(small_cfg.generator, chunk_rows=5000))
    digests = []
    for cfg in (small_cfg, big):
        c = migrated(cfg)
        load.run(c, cfg, "smoke")
        digests.append(table_digest(c))
    assert digests[0] == digests[1]


def test_insert_path_matches_copy(migrated: Migrated, small_cfg: config.Config) -> None:
    no_copy = replace(small_cfg, postgres=replace(small_cfg.postgres, bulk_copy=False))
    digests = []
    for cfg in (small_cfg, no_copy):
        c = migrated(cfg)
        load.run(c, cfg, "smoke")
        digests.append(table_digest(c))
    assert digests[0] == digests[1]


def test_failure_leaves_only_whole_chunks_and_rerun_completes_identically(
    migrated: Migrated, small_cfg: config.Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing chunk commits nothing; the ledger says exactly what is durable; re-running skips it (T10)."""
    clean = migrated(small_cfg)
    load.run(clean, small_cfg, "smoke")
    expected = table_digest(clean)

    real = generate.generate_chunk

    def boom(world: generate.World, entity: str, chunk_no: int, chunk_rows: int) -> generate.Chunk:
        chunk = real(world, entity, chunk_no, chunk_rows)
        if entity == "orders" and chunk_no == 2:
            # rows that would violate a CHECK mid-COPY: the whole chunk must roll back
            bad = chunk.tables["payment_attempts"][-1]._replace(amount=-1)  # type: ignore[attr-defined]
            chunk.tables["payment_attempts"][-1] = bad
        return chunk

    c = migrated(small_cfg)
    monkeypatch.setattr(generate, "generate_chunk", boom)
    with pytest.raises(psycopg.errors.CheckViolation):
        load.run(c, small_cfg, "smoke")
    state, error = c.execute("SELECT state, error FROM generator_meta.generation_runs").fetchone() or ("", "")
    assert state == "FAILED" and "CheckViolation" in error
    assert one(c, "SELECT count(*) FROM generator_meta.generation_chunks WHERE entity = 'orders'") == 2
    assert one(c, "SELECT count(*) FROM commerce.orders") == 600
    assert one(c, "SELECT max(substr(order_id, 5)::int) FROM commerce.orders") == 600  # chunks 0-1 only
    assert one(c, "SELECT count(*) FROM commerce.order_items WHERE order_id NOT IN"
                  " (SELECT order_id FROM commerce.orders)") == 0
    assert reconciliation_errors(c) == 0

    monkeypatch.setattr(generate, "generate_chunk", real)
    run_id = str(one(c, "SELECT run_id FROM generator_meta.generation_runs"))
    assert load.write_chunks(c, small_cfg, run_id, load.world_for(small_cfg, "smoke")) == (2, 7)
    assert table_digest(c) == expected
    assert load.write_chunks(c, small_cfg, run_id, load.world_for(small_cfg, "smoke")) == (0, 9)
    assert table_digest(c) == expected


def test_refuses_uninitialized_database(db_url: str, cfg: config.Config) -> None:
    with psycopg.connect(db_url, autocommit=True) as c, pytest.raises(load.LoadError, match="init-db"):
        load.run(c, cfg, "smoke")


def test_refuses_concurrent_run(aconn: psycopg.Connection, db_url: str, small_cfg: config.Config) -> None:
    with psycopg.connect(db_url, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(%s)", (load._RUN_LOCK_ID,))
        with pytest.raises(load.LoadError, match="another generate/resume"):
            load.run(aconn, small_cfg, "smoke")
    assert one(aconn, "SELECT count(*) FROM generator_meta.generation_runs") == 0


def test_requires_autocommit(aconn: psycopg.Connection, small_cfg: config.Config) -> None:
    aconn.autocommit = False
    with pytest.raises(load.LoadError, match="autocommit"):
        load.run(aconn, small_cfg, "smoke")


def test_refuses_rows_without_a_run(aconn: psycopg.Connection, small_cfg: config.Config) -> None:
    aconn.execute("INSERT INTO commerce.categories VALUES ('cat_1', NULL, 'stray')")
    with pytest.raises(load.LoadError, match="no run recorded"):
        load.run(aconn, small_cfg, "smoke")
    assert one(aconn, "SELECT count(*) FROM generator_meta.generation_runs") == 0


def test_config_profile_does_not_override_config_counts(aconn: psycopg.Connection, small_cfg: config.Config) -> None:
    cfg = replace(small_cfg, generator=replace(small_cfg.generator, profile="smoke",
                                               counts={**small_cfg.generator.counts, "orders": 1234}))
    result = load.run(aconn, cfg)  # no explicit preset: config counts win, profile is just the label
    assert result.counts["orders"] == 1234
    assert one(aconn, "SELECT profile FROM generator_meta.generation_runs") == "smoke"
