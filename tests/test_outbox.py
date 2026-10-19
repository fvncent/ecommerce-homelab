"""Outbox events without a database: one per order / payment state transition, consistent with the rows, the same
in one shot and in any clock steps (with PostgreSQL: integration/test_outbox.py)."""

import json
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ecomgen import generate as g

START, END = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 15, tzinfo=UTC)
WORLD = g.World(seed=42, clock_start=START, clock_end=END, sellers=12, customers=150, products=80, orders=400)
# Large enough that every path of _lifecycle (failed payments, cancellations, deliveries) occurs.
BIG = replace(WORLD, seed=5, orders=3000, clock_end=START + timedelta(days=30))


def one_shot(world: g.World, chunk_rows: int = 64) -> tuple[list[g.OutboxEvent], dict[str, tuple[object, ...]]]:
    """(events, rows by order_id / payment_id) of a generate run."""
    events: list[g.OutboxEvent] = []
    rows: dict[str, tuple[object, ...]] = {}
    for chunk in g.generate_all(world, chunk_rows):
        events += [e for e in chunk.tables.get("outbox_events", []) if isinstance(e, g.OutboxEvent)]
        for t, key in (("orders", "order_id"), ("payment_attempts", "payment_id")):
            rows.update((getattr(r, key), r) for r in chunk.tables.get(t, []))
    return events, rows


def stepped(world: g.World, stops: list[datetime], chunk_rows: int = 37) -> list[list[g.OutboxEvent]]:
    """The events of each step, in step order."""
    out, previous = [], None
    for as_of in stops:
        plan = g.plan_step(world, previous, as_of)
        out.append([e for c in range(g.step_chunk_count(plan, chunk_rows))
                    for e in g.step_chunk(world, plan, c, chunk_rows).events])
        previous = as_of
    return out


def by_aggregate(events: list[g.OutboxEvent]) -> dict[str, list[g.OutboxEvent]]:
    agg: dict[str, list[g.OutboxEvent]] = defaultdict(list)
    for e in events:
        agg[e.aggregate_id].append(e)
    return {k: sorted(v, key=lambda e: e.aggregate_seq) for k, v in agg.items()}


@pytest.fixture(scope="module")
def big() -> tuple[list[g.OutboxEvent], dict[str, tuple[object, ...]]]:
    return one_shot(BIG)


def test_each_aggregate_is_a_valid_transition_chain(big: tuple[list[g.OutboxEvent], dict[str, tuple[object, ...]]]
                                                    ) -> None:
    """seq 1 = creation, then one event per transition of the lifecycle state machine, strictly later each time."""
    events, _ = big
    seen: dict[str, set[tuple[str, str]]] = {"order": set(), "payment": set()}
    for agg_id, chain in by_aggregate(events).items():
        assert [e.aggregate_seq for e in chain] == list(range(1, len(chain) + 1)), agg_id
        assert all(e.event_id == f"obx_{agg_id}_{e.aggregate_seq}" for e in chain)
        first, *rest = chain
        assert first.event_type == ("OrderPlaced" if first.aggregate_type == "order" else "PaymentAttemptCreated")
        assert json.loads(first.payload)["from"] is None
        for prev, e in zip(chain, rest):
            p = json.loads(e.payload)
            assert e.event_type in ("OrderStatusChanged", "PaymentStateChanged")
            assert p["from"] == json.loads(prev.payload)["to"] and prev.occurred_at < e.occurred_at
            seen[e.aggregate_type].add((p["from"], p["to"]))
        assert {e.aggregate_type for e in chain} == {"payment" if agg_id.startswith("pay_") else "order"}
        owner = agg_id if agg_id.startswith("ord_") else f"ord_{(int(agg_id[4:]) - 1) // g.MAX_ATTEMPTS + 1}"
        assert all(e.order_id == owner and e.payment_id == (None if agg_id == owner else agg_id) for e in chain)
        assert all(e.schema_version == g.OUTBOX_SCHEMA_VERSION and e.occurred_at.tzinfo == UTC for e in chain)
    assert seen == {"order": set(g.ORDER_TRANSITIONS), "payment": set(g.PAYMENT_TRANSITIONS)}  # every valid one


def test_the_history_reconstructs_every_row(big: tuple[list[g.OutboxEvent], dict[str, tuple[object, ...]]]) -> None:
    """Replaying an aggregate's events gives its row's current status and updated_at; no row lacks events."""
    events, rows = big
    chains = by_aggregate(events)
    assert set(chains) == set(rows)
    for agg_id, chain in chains.items():
        row = rows[agg_id]
        state = getattr(row, "status", None) or getattr(row, "state")
        assert (json.loads(chain[-1].payload)["to"], chain[-1].occurred_at) == (state, getattr(row, "updated_at"))
        created = row.placed_at if isinstance(row, g.Order) else getattr(row, "created_at")
        assert chain[0].occurred_at == created
        if isinstance(row, g.PaymentAttempt):
            assert chain[0].payment_id == row.payment_id and chain[0].order_id == row.order_id


def test_order_placed_carries_the_reconciled_order(big: tuple[list[g.OutboxEvent], dict[str, tuple[object, ...]]]
                                                  ) -> None:
    events, rows = big
    for e in events:
        if e.event_type == "OrderPlaced":
            p, row = json.loads(e.payload), rows[e.order_id]
            assert isinstance(row, g.Order) and p["total"] == str(row.total) and p["customer_id"] == row.customer_id
            assert sum(Decimal(i["line_total"]) for i in p["items"]) + Decimal(p["shipping"]) == Decimal(p["total"])
        elif e.aggregate_type == "payment":
            assert json.loads(e.payload)["amount"] == str(getattr(rows[e.order_id], "total"))


def test_events_are_deterministic_and_independent_of_chunking() -> None:
    a, _ = one_shot(WORLD, 64)
    b, _ = one_shot(WORLD, 7)
    assert a == b and len({e.event_id for e in a}) == len(a)
    assert one_shot(replace(WORLD, seed=43))[0] != a


@pytest.mark.parametrize("days", [[14], list(range(1, 15)), [0, 0.5, 3.25, 9, 14]])
def test_steps_together_write_exactly_the_one_shot_events(days: list[float]) -> None:
    whole, _ = one_shot(WORLD)
    stops = [START + timedelta(days=d) for d in days]
    steps = stepped(WORLD, stops)
    together = [e for s in steps for e in s]
    assert sorted(together) == sorted(whole) and len({e.event_id for e in together}) == len(together)
    for prev, as_of, events in zip([None, *stops], stops, steps):
        assert all((prev is None or e.occurred_at >= prev) and e.occurred_at < as_of for e in events)


def test_a_step_records_every_transition_it_spans_not_just_the_end_state() -> None:
    """One step over an order's whole life: its row jumps from nothing to DELIVERED, its events show each stage."""
    n = next(k for k in range(1, WORLD.orders) if WORLD.order(k)[0].status == "DELIVERED")
    placed, delivered = WORLD.placed_at(n), WORLD.order(n)[0].updated_at
    steps = stepped(WORLD, [placed - timedelta(seconds=1), delivered + timedelta(seconds=1), END])
    mine = [e for e in steps[1] if e.order_id == f"ord_{n}" and e.aggregate_type == "order"]
    assert [json.loads(e.payload)["to"] for e in mine] == ["CREATED", "CONFIRMED", "PROCESSING", "SHIPPED", "DELIVERED"]
    assert not [e for s in (steps[0], steps[2]) for e in s if e.order_id == f"ord_{n}"]


def test_an_event_at_exactly_as_of_belongs_to_the_next_step() -> None:
    whole, _ = one_shot(WORLD)
    t = sorted(e.occurred_at for e in whole)[len(whole) // 2]
    first, second = stepped(WORLD, [t, END])
    at_t = [e for e in whole if e.occurred_at == t]
    assert at_t and not [e for e in first if e.occurred_at == t] and all(e in second for e in at_t)


def test_an_empty_window_has_no_events() -> None:
    assert stepped(WORLD, [START])[0] == []
    quiet = replace(WORLD, orders=1)
    last = max(e.occurred_at for e in one_shot(quiet)[0])
    assert stepped(quiet, [last + timedelta(seconds=1), END])[1] == []
