"""Clock steps without a database: stepping to clock_end in any number of steps gives exactly the generate rows
(with PostgreSQL: integration/test_advance.py)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ecomgen import generate as g

START, END = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 15, tzinfo=UTC)
WORLD = g.World(seed=42, clock_start=START, clock_end=END, sellers=12, customers=150, products=80, orders=400)
KEYS = {"orders": "order_id", "order_items": "order_item_id", "payment_attempts": "payment_id"}


def historical(world: g.World) -> dict[str, dict[str, tuple[object, ...]]]:
    out: dict[str, dict[str, tuple[object, ...]]] = {t: {} for t in KEYS}
    for chunk in g.generate_all(world, 64):
        for table, rs in chunk.tables.items():
            if table in KEYS:
                out[table].update((getattr(r, KEYS[table]), r) for r in rs)
    return out


def apply_steps(world: g.World, stops: list[datetime], chunk_rows: int = 37,
                state: dict[str, dict[str, tuple[object, ...]]] | None = None) -> dict[str, dict[str, tuple[object, ...]]]:
    """Apply each step's chunks like the loader: inserts must be new, updates must start from the stored row."""
    state = state if state is not None else {t: {} for t in KEYS}
    previous = None
    for as_of in stops:
        plan = g.plan_step(world, previous, as_of)
        for c in range(g.step_chunk_count(plan, chunk_rows)):
            chunk = g.step_chunk(world, plan, c, chunk_rows)
            for table, rs in chunk.inserts.items():
                for r in rs:
                    key = getattr(r, KEYS[table])
                    assert key not in state[table], f"{key} inserted twice"
                    state[table][key] = r
            for table, pairs in (("orders", chunk.order_updates), ("payment_attempts", chunk.payment_updates)):
                for before, after in pairs:
                    key = getattr(after, KEYS[table])
                    assert state[table][key] == before, f"{key} did not hold its previous state"
                    assert before != after
                    state[table][key] = after
        previous = as_of
    return state


@pytest.mark.parametrize("stops", [
    [END],
    [START + timedelta(days=d) for d in range(1, 15)],  # daily
    [START, START + timedelta(hours=1), START + timedelta(days=9, seconds=1), END],  # uneven, incl. an empty start
    [START + timedelta(hours=h) for h in range(6, 14 * 24 + 1, 6)],  # every 6 h
])
def test_any_steps_to_clock_end_equal_generate(stops: list[datetime]) -> None:
    assert apply_steps(WORLD, stops) == historical(WORLD)


def test_state_after_each_step_equals_the_world_as_of_that_time() -> None:
    stops = [START + timedelta(days=2, hours=5), START + timedelta(days=6), START + timedelta(days=11)]
    for k, as_of in enumerate(stops, 1):
        expected: dict[str, dict[str, tuple[object, ...]]] = {t: {} for t in KEYS}
        for n in range(1, WORLD.orders_by(as_of) + 1):
            order, items, payments = WORLD.order(n, as_of)
            expected["orders"][order.order_id] = order
            expected["order_items"].update((i.order_item_id, i) for i in items)
            expected["payment_attempts"].update((p.payment_id, p) for p in payments)
        assert expected["orders"] and apply_steps(WORLD, stops[:k]) == expected


def test_lifecycle_max_bounds_every_order() -> None:
    """Every change is at or before placed + LIFECYCLE_MAX, so the state just after it is final (= a year later)."""
    for world in (WORLD, replace(WORLD, seed=7, orders=2000)):
        for n in range(1, world.orders + 1):
            placed = world.placed_at(n)
            final = placed + g.LIFECYCLE_MAX + timedelta(seconds=1)
            assert world.order(n, final) == world.order(n, placed + timedelta(days=365)), n


def test_orders_by_matches_placed_at() -> None:
    placed = [WORLD.placed_at(n) for n in range(1, WORLD.orders + 1)]
    assert placed == sorted(placed)
    for t in (START, placed[0], placed[0] - timedelta(seconds=1), placed[199], END):
        assert WORLD.orders_by(t) == sum(p < t for p in placed)
    crowded = replace(WORLD, clock_end=START + timedelta(seconds=100))  # more orders than seconds: equal times
    times = [crowded.placed_at(n) for n in range(1, crowded.orders + 1)]
    assert times == sorted(times) and crowded.orders_by(times[150]) == sum(p < times[150] for p in times)


def test_order_as_of_clock_end_is_the_default_and_unplaced_orders_are_refused() -> None:
    assert WORLD.order(7) == WORLD.order(7, END)
    with pytest.raises(g.GenerationError, match="not before"):
        WORLD.order(WORLD.orders, START)
    with pytest.raises(g.GenerationError, match="not before"):  # placed exactly at as_of: not there yet
        WORLD.order(7, WORLD.placed_at(7))


def test_a_step_revisits_only_orders_that_can_still_change() -> None:
    previous, as_of = START + timedelta(days=12), END
    plan = g.plan_step(WORLD, previous, as_of)
    assert plan.orders.start == WORLD.orders_by(previous - g.LIFECYCLE_MAX) + 1 > 1
    assert plan.orders.stop - 1 == WORLD.orders == WORLD.orders_by(as_of)
    assert plan.known == WORLD.orders_by(previous) < WORLD.orders
    assert g.plan_step(WORLD, None, as_of).orders == range(1, WORLD.orders + 1)


@pytest.mark.parametrize("previous, as_of, match", [
    (None, START - timedelta(seconds=1), "outside"),
    (None, END + timedelta(seconds=1), "outside"),
    (START + timedelta(days=2), START + timedelta(days=2), "not after"),
    (START + timedelta(days=2), START + timedelta(days=1), "not after"),
    (None, datetime(2026, 1, 2), "UTC"),
    (None, START + timedelta(days=1, microseconds=500_000), "whole second"),  # text and time windows would disagree
])
def test_step_bounds(previous: datetime | None, as_of: datetime, match: str) -> None:
    with pytest.raises(g.GenerationError, match=match):
        g.check_step(WORLD, previous, as_of)


def test_empty_step_has_no_chunks() -> None:
    plan = g.plan_step(WORLD, None, START)
    assert g.step_chunk_count(plan, 10) == 0
    with pytest.raises(g.GenerationError, match="out of range"):
        g.step_chunk(WORLD, plan, 0, 10)


def test_a_change_at_exactly_as_of_belongs_to_the_next_step() -> None:
    """Windows are [previous, as_of): an order placed, or a status / payment change made, at exactly a step's as_of is
    not in that step's state but in the next one; stepping through those very instants still equals generate."""
    n = next(k for k in range(30, WORLD.orders) if WORLD.order(k)[0].updated_at > WORLD.placed_at(k))
    placed, (order, _, pays) = WORLD.placed_at(n), WORLD.order(n)
    change, pay_change = order.updated_at, pays[-1].updated_at
    assert placed < change < END and placed < pay_change < END
    assert n not in g.plan_step(WORLD, None, placed).orders and n in g.plan_step(WORLD, placed, change).orders
    before = WORLD.order(n, change)
    assert before[0].updated_at < change and WORLD.order(n, change + timedelta(seconds=1))[0] == order
    assert WORLD.order(n, pay_change)[2][-1].updated_at < pay_change
    stops = sorted({placed, change, pay_change}) + [END]
    for k, as_of in enumerate(stops):
        state = apply_steps(WORLD, stops[:k + 1])
        if as_of <= placed:
            assert order.order_id not in state["orders"]  # placed at as_of: inserted by the next step
        else:
            assert state["orders"][order.order_id] == WORLD.order(n, as_of)[0]
    assert apply_steps(WORLD, stops) == historical(WORLD)


def test_order_at_several_times_equals_each_alone() -> None:
    times = (START + timedelta(days=4), START + timedelta(days=6), END)
    for n in (1, 60, WORLD.orders_by(times[0])):
        assert WORLD.order_at(n, times) == [WORLD.order(n, t) for t in times]
