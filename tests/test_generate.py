import hashlib
import re
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from itertools import chain
from typing import Any

import pytest

from ecomgen import generate as g

WORLD = g.World(seed=42, clock_start=datetime(2026, 1, 1, tzinfo=UTC), clock_end=datetime(2026, 1, 15, tzinfo=UTC),
                sellers=12, customers=150, products=80, orders=400)
# Same patterns as the CHECK constraints in 0001_core_schema.sql
ID_PATTERNS = {"sellers": "sel", "categories": "cat", "customers": "cus", "products": "prd",
               "orders": "ord", "order_items": "itm", "payment_attempts": "pay"}
# Sparse: few sellers/products, so inactive entities and distinct-line exclusions force _probe's fallback scan.
SPARSE = replace(WORLD, seed=7, sellers=2, customers=5, products=3, orders=200)
# fingerprint of rows(WORLD) + rows(SPARSE), per generate.DATA_VERSION. NEVER edit an existing entry for a data change:
# bump DATA_VERSION and add an entry (resume refuses runs from another data version; an edited entry would let it
# mix old and new rows). Editing is only right when the test's inputs change, not the generator's output.
GOLDEN = {1: "6747ce23f4146509c9294cb0b2a755c2a35ce2e3ddb10666f5ce9bbf516776d4",
          2: "6747ce23f4146509c9294cb0b2a755c2a35ce2e3ddb10666f5ce9bbf516776d4",  # 2 changed files only
          3: "6747ce23f4146509c9294cb0b2a755c2a35ce2e3ddb10666f5ce9bbf516776d4",  # 3 changed session times only
          4: "6747ce23f4146509c9294cb0b2a755c2a35ce2e3ddb10666f5ce9bbf516776d4"}  # 4: step windows; one-shot same
ORDER_FLOW = ["CREATED", "CONFIRMED", "PROCESSING", "SHIPPED", "DELIVERED"]


def rows(world: g.World = WORLD, chunk_rows: int = 64) -> dict[str, list[tuple[object, ...]]]:
    out: dict[str, list[tuple[object, ...]]] = {}
    for chunk in g.generate_all(world, chunk_rows):
        for table, rs in chunk.tables.items():
            out.setdefault(table, []).extend(rs)
    return out


@pytest.fixture(scope="module")
def data() -> dict[str, list[tuple[object, ...]]]:
    return rows()


def fingerprint(data: dict[str, list[tuple[object, ...]]]) -> str:
    return hashlib.sha256(repr(sorted(data.items())).encode()).hexdigest()


def test_same_seed_same_data_and_independent_of_chunk_size(data: dict[str, list[tuple[object, ...]]]) -> None:
    assert rows(chunk_rows=7) == data
    assert rows(chunk_rows=10_000) == data


def test_different_seed_different_data(data: dict[str, list[tuple[object, ...]]]) -> None:
    assert rows(replace(WORLD, seed=43)) != data


def test_golden_fingerprint(data: dict[str, list[tuple[object, ...]]]) -> None:
    # Changes when generated data changes: see the GOLDEN comment (bump DATA_VERSION, don't edit an entry).
    assert max(GOLDEN) == g.DATA_VERSION
    assert fingerprint({**data, **{f"sparse.{t}": r for t, r in rows(SPARSE).items()}}) == GOLDEN[g.DATA_VERSION]


def test_chunk_regenerates_alone(data: dict[str, list[tuple[object, ...]]]) -> None:
    chunk = g.generate_chunk(WORLD, "orders", 3, 50)
    assert (chunk.first, chunk.last) == (151, 200)
    assert chunk.tables["orders"] == data["orders"][150:200]


def test_chunk_bounds() -> None:
    assert g.chunk_count(WORLD, "orders", 64) == 7
    assert g.generate_chunk(WORLD, "orders", 6, 64).last == 400
    with pytest.raises(g.GenerationError, match="out of range"):
        g.generate_chunk(WORLD, "orders", 7, 64)
    with pytest.raises(g.GenerationError, match="unknown entity"):
        g.generate_chunk(WORLD, "refunds", 0, 64)


def test_ids_canonical_and_unique(data: dict[str, list[tuple[object, ...]]]) -> None:
    for table, prefix in ID_PATTERNS.items():
        ids = [r[0] for r in data[table]]
        assert all(re.fullmatch(rf"{prefix}_[1-9][0-9]*", str(i)) for i in ids), table
        assert len(set(ids)) == len(ids), table
    assert len(data["orders"]) == 400 and len(data["customers"]) == 150


def test_foreign_keys_resolve_in_generation_order(data: dict[str, list[tuple[object, ...]]]) -> None:
    sellers = {s.seller_id: s for s in data["sellers"] if isinstance(s, g.Seller)}
    customers = {c.customer_id: c for c in data["customers"] if isinstance(c, g.Customer)}
    products = {p.product_id: p for p in data["products"] if isinstance(p, g.Product)}
    seen: set[str] = set()
    for c in data["categories"]:
        assert isinstance(c, g.Category)
        assert c.parent_category_id is None or c.parent_category_id in seen  # parent written first: no cycles
        seen.add(c.category_id)
    for p in products.values():
        assert p.seller_id in sellers and p.category_id in seen
        assert p.created_at >= sellers[p.seller_id].created_at
        if sellers[p.seller_id].status != "ACTIVE":
            assert not p.active
    orders = {o.order_id: o for o in data["orders"] if isinstance(o, g.Order)}
    for o in orders.values():
        assert customers[o.customer_id].status == "ACTIVE"
        assert customers[o.customer_id].created_at < o.placed_at
    for i in data["order_items"]:
        assert isinstance(i, g.OrderItem)
        assert i.order_id in orders and products[i.product_id].active
        assert i.unit_price == products[i.product_id].price
    assert all(p[1] in orders for p in data["payment_attempts"])


def test_money_reconciles(data: dict[str, list[tuple[object, ...]]]) -> None:
    items: dict[str, list[g.OrderItem]] = {}
    for i in data["order_items"]:
        assert isinstance(i, g.OrderItem)
        items.setdefault(i.order_id, []).append(i)
        assert i.line_total == i.quantity * i.unit_price - i.discount + i.tax
        assert 0 <= i.discount <= i.quantity * i.unit_price
    money = [v for row in chain.from_iterable(data.values()) for v in row if isinstance(v, Decimal)]
    assert money and all(v.as_tuple().exponent == -2 for v in money)  # 2 dp, as NUMERIC(_, 2) stores them
    for o in data["orders"]:
        assert isinstance(o, g.Order)
        its = items[o.order_id]
        assert [i.line_no for i in its] == list(range(1, len(its) + 1))
        assert len({i.product_id for i in its}) == len(its)
        assert o.subtotal == sum(i.quantity * i.unit_price for i in its)
        assert o.discount == sum(i.discount for i in its)
        assert o.tax == sum(i.tax for i in its)
        assert o.total == o.subtotal - o.discount + o.tax + o.shipping > 0
        assert o.shipping == (0 if o.subtotal - o.discount >= g.FREE_SHIPPING_FROM else g.SHIPPING_FEE)
    for p in data["payment_attempts"]:
        assert isinstance(p, g.PaymentAttempt)
        assert p.currency == g.CURRENCY
    orders = {o[0]: o for o in data["orders"] if isinstance(o, g.Order)}
    assert all(p[6] == orders[str(p[1])].total for p in data["payment_attempts"])


def test_lifecycle_consistent_as_of_clock_end(data: dict[str, list[tuple[object, ...]]]) -> None:
    payments: dict[str, list[g.PaymentAttempt]] = {}
    for p in data["payment_attempts"]:
        assert isinstance(p, g.PaymentAttempt)
        payments.setdefault(p.order_id, []).append(p)
        assert p.created_at <= p.updated_at <= WORLD.clock_end
    statuses = set()
    for o in data["orders"]:
        assert isinstance(o, g.Order)
        statuses.add(o.status)
        assert WORLD.clock_start <= o.placed_at <= o.updated_at <= WORLD.clock_end
        ps = payments.get(o.order_id, [])
        assert [p.attempt_no for p in ps] == list(range(1, len(ps) + 1))
        assert all(p.state == "FAILED" for p in ps[:-1])  # only the last attempt may be live
        assert all(a.updated_at <= b.created_at for a, b in zip(ps, ps[1:]))
        last = ps[-1].state if ps else None
        if o.status == "CREATED":
            assert last in (None, "INITIATED", "FAILED")
        elif o.status == "CANCELLED":
            assert last in ("FAILED", "VOIDED")
        elif o.status == "CONFIRMED":
            assert last == "AUTHORIZED"
        else:
            assert ORDER_FLOW.index(o.status) >= 2 and last in ("AUTHORIZED", "CAPTURED", "SETTLED")
            if o.status in ("SHIPPED", "DELIVERED"):
                assert last in ("CAPTURED", "SETTLED")
    assert {"DELIVERED", "CANCELLED"} <= statuses


def test_late_orders_are_not_yet_delivered() -> None:
    # The last order is placed just before clock_end, so it can't have finished its lifecycle.
    order, _, _ = WORLD.order(WORLD.orders)
    assert order.status in ("CREATED", "CONFIRMED", "PROCESSING", "CANCELLED")


def test_synthetic_pii(data: dict[str, list[tuple[object, ...]]]) -> None:
    assert all(re.fullmatch(r"[^@\s]+@example\.com", str(c[2])) for c in data["customers"])


def test_no_eligible_customers_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    original = g.World.customer
    monkeypatch.setattr(g.World, "customer", lambda self, n: original(self, n)._replace(status="CLOSED"))
    with pytest.raises(g.GenerationError, match="no ACTIVE customers"):
        WORLD.order(1)



def test_choice_among_eligible_is_uniform(monkeypatch: pytest.MonkeyPatch) -> None:
    # A run of ineligible IDs must not inflate the next eligible one (linear probing would give cus_11 ~half).
    original = g.World.customer
    monkeypatch.setattr(g.World, "customer",
                        lambda self, n: original(self, n)._replace(status="CLOSED" if 2 <= n <= 10 else "ACTIVE"))
    world = replace(WORLD, customers=20, orders=2200)
    counts: dict[str, int] = {}
    for n in range(1, world.orders + 1):
        cid = world.order(n)[0].customer_id
        counts[cid] = counts.get(cid, 0) + 1
    assert len(counts) == 11 and max(counts.values()) < 1.5 * 200


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"clock_start": datetime(2026, 1, 1)}, "must be a UTC timestamp"),
        ({"clock_end": datetime(2025, 1, 1, tzinfo=UTC)}, "after clock_start"),
        ({"sellers": 0}, "sellers must be >= 1"),
    ],
)
def test_world_rejects_invalid_inputs(changes: dict[str, object], match: str) -> None:
    with pytest.raises(g.GenerationError, match=match):
        replace(WORLD, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize("chunk_rows", [0, -1])
def test_chunk_rows_must_be_positive(chunk_rows: int) -> None:
    with pytest.raises(g.GenerationError, match="chunk_rows must be >= 1"):
        list(g.generate_all(WORLD, chunk_rows))


def test_sparse_world_exercises_fallback_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    real = g._scan

    def counting(*args: Any) -> Any:
        nonlocal calls
        calls += 1
        return real(*args)

    monkeypatch.setattr(g, "_scan", counting)
    rows(SPARSE)
    assert calls  # so the golden fingerprint covers the fallback path too
