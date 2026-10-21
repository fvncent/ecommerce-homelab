"""Inventory without a database: movements follow each order's history, stock is never negative as of any time or
after any committed chunk, and clock steps together write exactly the one-shot movements (with PostgreSQL:
integration/test_inventory_db.py). Also: worlds recorded before warehouses existed still read."""

import dataclasses
import json
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ecomgen import generate as g
from ecomgen import load

START, END = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 15, tzinfo=UTC)
WORLD = g.World(seed=42, clock_start=START, clock_end=END, sellers=12, customers=150, products=80, orders=400)
BIG = replace(WORLD, seed=5, orders=3000, clock_end=START + timedelta(days=30))
# Few products in one warehouse: every cell sees heavy demand, so the opening bound is what keeps stock >= 0.
TIGHT = replace(WORLD, seed=9, sellers=1, products=4, orders=1500, warehouses=1)
Cell = tuple[str, str]


def one_shot(world: g.World, chunk_rows: int = 64) -> list[g.InventoryMovement]:
    return [m for c in g.generate_all(world, chunk_rows)
            for m in c.tables.get("inventory_movements", []) if isinstance(m, g.InventoryMovement)]


def apply(stock: dict[Cell, list[int]], moves: list[g.InventoryMovement]) -> None:
    """Add movements to (on_hand, reserved) per cell, like the loader, and check the schema's CHECK after it."""
    for m in moves:
        cell = stock.setdefault((m.warehouse_id, m.product_id), [0, 0])
        cell[0] += m.on_hand_delta
        cell[1] += m.reserved_delta
    assert all(0 <= r <= h for h, r in stock.values())


def stepped(world: g.World, stops: list[datetime], chunk_rows: int = 37) -> list[list[g.InventoryMovement]]:
    """Each step's movements in the loader's chunk order (inventory chunks, then orders), checking stock after every
    chunk, as each commits on its own."""
    out: list[list[g.InventoryMovement]] = []
    previous: datetime | None = None
    stock: dict[Cell, list[int]] = {}
    for as_of in stops:
        plan = g.plan_step(world, previous, as_of)
        step: list[g.InventoryMovement] = []
        chunks = [g.generate_chunk(world, "inventory", c, chunk_rows, (previous, as_of)).tables
                  for c in range(g.chunk_count(world, "inventory", chunk_rows))]
        chunks += [g.step_chunk(world, plan, c, chunk_rows).inserts
                   for c in range(g.step_chunk_count(plan, chunk_rows))]
        for tables in chunks:
            moves = [m for m in tables["inventory_movements"] if isinstance(m, g.InventoryMovement)]
            apply(stock, moves)
            step += moves
        out.append(step)
        previous = as_of
    return out


@pytest.fixture(scope="module")
def big() -> list[g.InventoryMovement]:
    return one_shot(BIG)


@pytest.mark.parametrize("world", [BIG, TIGHT], ids=["big", "tight"])
def test_stock_is_never_negative_as_of_any_time(world: g.World) -> None:
    """Applying movements in time order (all those at one instant together), on_hand >= reserved >= 0 throughout."""
    by_time: dict[datetime, list[g.InventoryMovement]] = defaultdict(list)
    for m in one_shot(world):
        by_time[m.occurred_at].append(m)
    stock: dict[Cell, list[int]] = {}
    for t in sorted(by_time):
        apply(stock, by_time[t])
    assert any(r for _, r in stock.values()) and min(h - r for h, r in stock.values()) >= 0


def test_each_line_moves_with_its_order_history(big: list[g.InventoryMovement]) -> None:
    """RESERVE at confirmation, then RELEASE at a cancellation after it or SHIP at shipping; nothing for an order that
    was never confirmed."""
    moves: dict[str, dict[str, datetime]] = defaultdict(dict)
    for m in big:
        if m.order_item_id is not None:
            assert m.movement_type not in moves[m.order_item_id]
            moves[m.order_item_id][m.movement_type] = m.occurred_at
    kinds: set[tuple[str, ...]] = set()
    for n in range(1, BIG.orders + 1):
        _, events, _ = BIG.order_history(n, (BIG.clock_end,), (None, BIG.clock_end))
        status = {json.loads(e.payload)["to"]: e.occurred_at for e in events if e.event_type == "OrderStatusChanged"}
        expected = {}
        if "CONFIRMED" in status:
            expected["RESERVE"] = status["CONFIRMED"]
            if "CANCELLED" in status:
                expected["RELEASE"] = status["CANCELLED"]
            if "SHIPPED" in status:
                expected["SHIP"] = status["SHIPPED"]
        _, items, _ = BIG.order(n)
        for i in items:
            assert moves.get(i.order_item_id, {}) == expected, i.order_item_id
        kinds.add(tuple(sorted(expected)))
    assert {(), ("RESERVE",), ("RELEASE", "RESERVE"), ("RESERVE", "SHIP")} <= kinds  # every path occurs


def test_a_line_ships_from_a_stocking_warehouse_in_the_customers_region_when_there_is_one() -> None:
    regions = {w: BIG.warehouse(w).region for w in range(1, BIG.warehouses + 1)}
    assert sorted(regions.values()) == ["APAC", "EU", "NA"]
    for n in range(1, 300):
        d = BIG._draws(n)
        for item, w in d.lines:
            stocked = BIG.stocked_at(int(item.product_id.removeprefix("prd_")))
            local = [s for s in stocked if regions[s] == g.COUNTRY_REGION[d.customer.country]]
            assert w == (local[0] if local else stocked[0])


def test_receipts_open_every_cell_before_the_clock_and_replenishments_are_weekly(big: list[g.InventoryMovement]
                                                                                ) -> None:
    receipts = {(m.warehouse_id, m.product_id): m for m in big if m.movement_type == "RECEIPT"}
    cells = {(f"wh_{w}", f"prd_{p}") for p in range(1, BIG.products + 1) for w in BIG.stocked_at(p)}
    assert set(receipts) == cells and all(m.quantity > 0 and m.occurred_at < START for m in receipts.values())
    assert {(m.warehouse_id, m.product_id) for m in big} == cells  # every movement has its inventory row
    for m in big:
        if m.movement_type == "REPLENISHMENT":
            assert m.occurred_at in BIG.restock_times(int(m.warehouse_id.removeprefix("wh_")))
            assert m.movement_id.startswith(f"mov_rpl_{m.warehouse_id}_{m.product_id}_")


def test_movements_are_deterministic_and_independent_of_chunking(big: list[g.InventoryMovement]) -> None:
    assert sorted(one_shot(BIG, chunk_rows=500)) == sorted(big)
    assert len({m.movement_id for m in big}) == len(big)


@pytest.mark.parametrize("stops", [
    [START + timedelta(days=d) for d in range(1, 31)],  # daily
    [START + timedelta(hours=6), START + timedelta(days=7, hours=6), START + timedelta(days=8, seconds=1),
     START + timedelta(days=30)],  # one at exactly a delivery time
    [START + timedelta(days=30)],
], ids=["daily", "uneven", "one"])
def test_steps_together_write_exactly_the_one_shot_movements(stops: list[datetime], big: list[g.InventoryMovement]
                                                             ) -> None:
    steps = stepped(BIG, stops)
    assert sorted(m for s in steps for m in s) == sorted(big)
    for (previous, as_of), step in zip(zip([None, *stops], stops), steps):
        assert all((previous is None or m.occurred_at >= previous) and m.occurred_at < as_of for m in step)


def test_a_tight_world_stays_valid_after_every_committed_chunk() -> None:
    stepped(TIGHT, [START + timedelta(hours=h) for h in range(12, 14 * 24 + 1, 12)], chunk_rows=50)


def test_later_steps_never_need_the_opening_pass() -> None:
    """Only the first window (start None) writes receipts, which need _openings' pass over every order."""
    g._openings.cache_clear()
    g.generate_chunk(WORLD, "inventory", 0, 1000, (START + timedelta(days=1), START + timedelta(days=8)))
    assert g._openings.cache_info().misses == 0


def record(world: g.World, version: int, *missing: str) -> dict[str, object]:
    """A run's recorded resume fields as an older release wrote them: its data version, without the given fields."""
    fields = {f.name: getattr(world, f.name) for f in dataclasses.fields(world) if f.name not in missing}
    return {"data_version": version, "world": json.loads(json.dumps(fields, default=datetime.isoformat))}


def test_a_world_recorded_before_a_field_existed_reads_with_its_default() -> None:
    """validate (feeds) and status read old runs' records: a field added later takes its default."""
    assert load.recorded_world(record(BIG, g.DATA_VERSION)) == BIG
    assert load.recorded_world(record(BIG, 5, "warehouses")) == replace(BIG, warehouses=3)
    assert load.recorded_world(record(BIG, 1, "warehouses", "clickstream_events")) == replace(
        BIG, warehouses=3, clickstream_events=0)


@pytest.mark.parametrize(("version", "missing"), [
    (6, ("warehouses",)),  # data version 6 always records warehouses
    (2, ("clickstream_events",)),
    (5, ("seed",)),  # never optional
    (5, ("orders", "warehouses")),
])
def test_a_field_the_runs_version_required_is_never_defaulted(version: int, missing: tuple[str, ...]) -> None:
    with pytest.raises(load.LoadError, match="malformed"):
        load.recorded_world(record(BIG, version, *missing))
    with pytest.raises(load.LoadError, match="malformed"):  # nor without a data version to judge by
        load.recorded_world({"world": record(BIG, version)["world"]})


def test_every_defaulted_world_field_says_when_it_was_added() -> None:
    defaulted = {f.name for f in dataclasses.fields(g.World) if f.default is not dataclasses.MISSING}
    assert defaulted == set(g.WORLD_FIELD_SINCE) and max(g.WORLD_FIELD_SINCE.values()) <= g.DATA_VERSION
