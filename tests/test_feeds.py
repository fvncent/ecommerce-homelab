"""File feeds without a database: determinism, contracts, money and rollover.
Publishing with PostgreSQL: integration/test_publish.py."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ecomgen import config, feeds
from ecomgen import generate as g

EXAMPLE = Path(__file__).parents[1] / "config" / "generator.example.yaml"
WORLD = g.World(seed=42, clock_start=datetime(2026, 1, 1, tzinfo=UTC), clock_end=datetime(2026, 1, 15, tzinfo=UTC),
                sellers=10, customers=200, products=100, orders=600, clickstream_events=2003)
RUN = "run_20260101T000000Z_abcdef"
# sha256 over (relative path, file bytes) of every staged file of WORLD, per generate.DATA_VERSION. Same rules as
# GOLDEN in test_generate.py: never edit an entry for a data change; bump DATA_VERSION and add one.
FEED_GOLDEN = {2: "bfb7276d62db9a2f1487ea597baf383fe245c44457ad8d2d43ff1b0d39caf385",
               3: "af1de3311df79b3ccdbe07b8c24814df86ce2c5a3de8453c63a69527f733efc0",  # sessions in time slots
               4: "af1de3311df79b3ccdbe07b8c24814df86ce2c5a3de8453c63a69527f733efc0",  # windows [previous, as_of)
               5: "af1de3311df79b3ccdbe07b8c24814df86ce2c5a3de8453c63a69527f733efc0"}  # outbox: files unchanged


def cfg(chunk_rows: int = 250, max_mb: int = 128, csv: bool = True, jsonl: bool = True) -> config.Config:
    c = config.load(EXAMPLE)
    return replace(c, generator=replace(c.generator, chunk_rows=chunk_rows),
                   filesystem=replace(c.filesystem, max_file_size_mb=max_mb, csv=csv, jsonl=jsonl))


def staged(tmp: Path, c: config.Config, world: g.World = WORLD) -> tuple[dict[str, feeds.FileStat], dict[str, bytes]]:
    stats = feeds.write_staged(world, c, RUN, tmp)
    return stats, {rel: (tmp / rel).read_bytes() for rel in stats}


def fingerprint(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode() + b"\0" + files[rel] + b"\0")
    return h.hexdigest()


def payments(world: g.World = WORLD) -> list[g.PaymentAttempt]:
    return [p for n in range(1, world.orders + 1) for p in world.order(n)[2]]


def test_files_are_deterministic_and_independent_of_chunk_size(tmp_path: Path) -> None:
    _, a = staged(tmp_path / "a", cfg(chunk_rows=250))
    _, b = staged(tmp_path / "b", cfg(chunk_rows=7))
    _, c = staged(tmp_path / "c", cfg(chunk_rows=100_000))
    assert a == b == c
    _, other = staged(tmp_path / "d", cfg(), replace(WORLD, seed=43))
    assert other != a


def test_feed_golden_fingerprint(tmp_path: Path) -> None:
    _, files = staged(tmp_path, cfg())
    assert max(FEED_GOLDEN) == g.DATA_VERSION
    assert fingerprint(files) == FEED_GOLDEN[g.DATA_VERSION]


def test_layout_and_stats(tmp_path: Path) -> None:
    stats, files = staged(tmp_path, cfg())
    for rel, st in stats.items():
        day = rel.split("date=")[1].split("/")[0]
        assert rel.startswith(("source_feeds/payment_settlement/date=", "clickstream/date=")), rel
        assert f"/{RUN}-" in rel and rel.endswith(("-part-00000.csv", "-part-00000.jsonl"))
        records = list(feeds.iter_records(tmp_path / rel, st.fmt))
        assert len(records) == st.records > 0
        assert st.start is not None and st.end is not None
        assert st.start.date().isoformat() == st.end.date().isoformat() == day  # partitioned by event date
    assert {st.feed for st in stats.values()} == {"payment_settlement", "clickstream"}


def test_settlements_cover_exactly_the_settled_payments(tmp_path: Path) -> None:
    stats, _ = staged(tmp_path, cfg())
    rows = [r for rel, st in stats.items() if st.feed == "payment_settlement"
            for r in feeds.iter_records(tmp_path / rel, "csv")]
    settled = {p.payment_id: p for p in payments() if p.state == "SETTLED"}
    assert settled and sorted(str(r["payment_id"]) for r in rows) == sorted(settled)
    for r in rows:
        p = settled[str(r["payment_id"])]
        gross, fee, net = (Decimal(str(r[k])) for k in ("gross_amount", "fee_amount", "net_amount"))
        assert (r["order_id"], gross, r["currency"], r["provider"]) == (p.order_id, p.amount, p.currency, p.provider)
        assert fee + net == gross and Decimal(0) <= fee <= gross and fee == fee.quantize(Decimal("0.01"))
        assert r["settled_at"] == feeds.ts(p.updated_at) and r["settlement_date"] == r["settled_at"][:10]
        assert r["batch_id"] == f"{p.provider}-{r['settlement_date']}"
        assert r["settlement_id"] == "stl_" + p.payment_id[4:]


def test_fee_is_exact_and_capped() -> None:
    p = payments()[0]
    row = feeds.settlement_row(p._replace(state="SETTLED", amount=Decimal("100.00"), provider="adyen-sim"))
    assert row and (row["fee_amount"], row["net_amount"]) == ("2.75", "97.25")
    tiny = feeds.settlement_row(p._replace(state="SETTLED", amount=Decimal("0.10"), provider="payco-sim"))
    assert tiny and (tiny["fee_amount"], tiny["net_amount"]) == ("0.10", "0.00")
    assert feeds.settlement_row(p._replace(state="CAPTURED")) is None


def test_clickstream_contract(tmp_path: Path) -> None:
    stats, _ = staged(tmp_path, cfg())
    events = [e for rel, st in stats.items() if st.feed == "clickstream"
              for e in feeds.iter_records(tmp_path / rel, "jsonl")]
    assert len(events) == WORLD.clickstream_events + WORLD.orders
    assert len({e["event_id"] for e in events}) == len(events)
    orders = {o.order_id: o for o in (WORLD.order(n)[0] for n in range(1, WORLD.orders + 1))}
    customers = {WORLD.customer(n).customer_id for n in range(1, WORLD.customers + 1)}
    products = {WORLD.product(n).product_id for n in range(1, WORLD.products + 1)}
    lo, hi = feeds.ts(WORLD.clock_start), feeds.ts(WORLD.clock_end)
    for e in events:
        assert set(e) == {"event_id", "schema_version", "event_type", "customer_id", "session_id", "product_id",
                          "event_time", "arrived_at", "source_system", "payload"}
        assert e["schema_version"] == 1 and e["source_system"] == "web_app"
        assert lo <= str(e["event_time"]) < hi and str(e["arrived_at"]) > str(e["event_time"])
        assert e["customer_id"] is None or e["customer_id"] in customers
        assert e["product_id"] is None or e["product_id"] in products
        if e["event_type"] == "order_placed":
            payload = e["payload"]
            assert isinstance(payload, dict)
            o = orders[str(payload["order_id"])]
            assert (e["event_id"], e["customer_id"], e["event_time"]) == (f"evt_{o.order_id}", o.customer_id,
                                                                          feeds.ts(o.placed_at))
            assert payload["total"] == str(o.total)
        else:
            assert e["event_type"] in {t for t, _ in feeds.BROWSE_TYPES}
            assert (e["product_id"] is None) == (e["event_type"] == "search")
    # sessions: one customer each, events in time order
    by_session: dict[object, list[dict[str, object]]] = {}
    for e in events:
        by_session.setdefault(e["session_id"], []).append(e)
    for s in (f"ses_{k}" for k in range(1, feeds.session_count(WORLD) + 1)):
        evs = sorted(by_session[s], key=lambda e: int(str(e["event_id"])[4:]))
        assert len({e["customer_id"] for e in evs}) == 1
        assert [e["event_time"] for e in evs] == sorted(str(e["event_time"]) for e in evs)
    assert len(by_session["ses_401"]) == 3  # 2003 events: the last session is partial


def test_rollover_splits_files_without_losing_records(tmp_path: Path) -> None:
    w = feeds._Writer(tmp_path, 2_000)  # a 2 kB limit (config's minimum is 1 MB) forces several parts
    recs = [(f"line {i}\n", datetime(2026, 1, 2, tzinfo=UTC)) for i in range(500)]
    w.add("clickstream", f"clickstream/date=2026-01-02/{RUN}", "jsonl", None, recs[:200])
    w.add("clickstream", f"clickstream/date=2026-01-02/{RUN}", "jsonl", None, recs[200:])
    parts = sorted(w.files)
    assert len(parts) > 1 and parts[0].endswith("-part-00000.jsonl")
    lines = [line for rel in parts for line in (tmp_path / rel).read_text().splitlines(keepends=True)]
    assert lines == [r[0] for r in recs] and sum(w.files[p].records for p in parts) == 500
    assert all((tmp_path / rel).stat().st_size <= 2_000 + 10 for rel in parts)


@pytest.mark.parametrize(("csv", "jsonl", "expected"), [
    (True, False, {"payment_settlement"}), (False, True, {"clickstream"}), (False, False, set()),
])
def test_feed_toggles(tmp_path: Path, csv: bool, jsonl: bool, expected: set[str]) -> None:
    stats, _ = staged(tmp_path, cfg(csv=csv, jsonl=jsonl))
    assert {st.feed for st in stats.values()} == expected


def test_tiny_window_keeps_events_inside_it(tmp_path: Path) -> None:
    world = replace(WORLD, clock_end=datetime(2026, 1, 1, 0, 10, tzinfo=UTC), orders=20, clickstream_events=50)
    stats, _ = staged(tmp_path, cfg(), world)
    events = [e for rel, st in stats.items() if st.feed == "clickstream"
              for e in feeds.iter_records(tmp_path / rel, "jsonl")]
    assert len(events) == 70 and all(str(e["event_time"]) < "2026-01-01T00:10:00Z" for e in events)


def _records(files: dict[str, bytes]) -> dict[str, list[bytes]]:
    """feed -> sorted record lines (CSV headers dropped), independent of file names and splitting."""
    out: dict[str, list[bytes]] = {"source_feeds": [], "clickstream": []}
    for rel, data in files.items():
        lines = data.splitlines(keepends=True)
        out[rel.split("/")[0]] += lines[1:] if rel.endswith(".csv") else lines
    return {k: sorted(v) for k, v in out.items()}


@pytest.mark.parametrize("days", [[14], list(range(1, 15)), [0, 0.5, 3.25, 9, 14], [h / 24 for h in [*range(1, 336, 7), 336]]])
def test_step_files_together_equal_the_one_shot_files(tmp_path: Path, days: list[float]) -> None:
    """Each step publishes only its window; every record lands in exactly one step, none is lost or doubled."""
    c = cfg()
    _, whole = staged(tmp_path / "whole", c)
    together: dict[str, bytes] = {}
    previous = None
    for k, d in enumerate(days):
        as_of = WORLD.clock_start + timedelta(days=d)
        plan = g.plan_step(WORLD, previous, as_of)
        run = f"run_20260101T00000{k}Z_abcdef"
        stats = feeds.write_staged(WORLD, c, run, tmp_path / run, plan)
        for rel, st in stats.items():
            assert rel.split("/")[-1].startswith(run)
            assert st.start is not None and st.end is not None and feeds.in_window(plan, st.start)
            assert feeds.in_window(plan, st.end)  # every record of a step's file is inside its window
            together[rel] = (tmp_path / run / rel).read_bytes()
        previous = as_of
    assert _records(together) == _records(whole) and _records(whole)["clickstream"]


def test_step_windows_are_half_open() -> None:
    t0, t1 = WORLD.clock_start + timedelta(days=1), WORLD.clock_start + timedelta(days=2)
    first, later = g.plan_step(WORLD, None, t0), g.plan_step(WORLD, t0, t1)
    assert feeds.in_window(first, WORLD.clock_start) and not feeds.in_window(first, t0)
    assert feeds.in_window(later, t0) and not feeds.in_window(later, t1)
    assert feeds.in_window(later, t1 - timedelta(seconds=1)) and not feeds.in_window(later, t0 - timedelta(seconds=1))


def test_browsing_events_by_counts_events_up_to_a_time(tmp_path: Path) -> None:
    _, files = staged(tmp_path, cfg())
    times = sorted(json.loads(line)["event_time"] for rel, data in files.items() if rel.startswith("clickstream/")
                   for line in data.splitlines() if b'"order_placed"' not in line)
    assert len(times) == WORLD.clickstream_events == feeds.browsing_events_by(WORLD, WORLD.clock_end, 250)
    t = WORLD.clock_start + timedelta(days=5)
    assert feeds.browsing_events_by(WORLD, t, 250) == sum(x < feeds.ts(t) for x in times) > 0


def _step_records(world: g.World, c: config.Config, tmp: Path,
                  stops: list[datetime]) -> list[tuple[datetime | None, datetime, str, str, dict[str, object]]]:
    """(previous, as_of, date partition, run, record) of every record each step publishes."""
    out: list[tuple[datetime | None, datetime, str, str, dict[str, object]]] = []
    previous: datetime | None = None
    for k, as_of in enumerate(stops):
        run = f"run_2026010{k}T000000Z_abcdef"
        for rel, st in feeds.write_staged(world, c, run, tmp / run, g.plan_step(world, previous, as_of)).items():
            date = rel.split("date=")[1].split("/")[0]
            out += [(previous, as_of, date, run, r) for r in feeds.iter_records(tmp / run / rel, st.fmt)]
        previous = as_of
    return out


def _at(record: dict[str, object]) -> str:
    return str(record.get("event_time") or record["settled_at"])


def test_records_at_exactly_a_step_end_belong_to_the_next_step(tmp_path: Path) -> None:
    """A browsing event, an order_placed event and a settlement stamped exactly at a step's as_of are published by
    the next step (whose window starts there), once; all steps together still equal the one-shot files."""
    settled = sorted(p.updated_at for p in payments() if p.state == "SETTLED")
    stops = sorted({datetime.fromisoformat(str(_browse(WORLD)[700]["event_time"])), WORLD.placed_at(300),
                    settled[len(settled) // 2]}) + [WORLD.clock_end]
    assert len(stops) == 4
    recs = _step_records(WORLD, cfg(), tmp_path, stops)
    for b in stops[:-1]:
        at_b = [(prev, run) for prev, _, _, run, r in recs if _at(r) == feeds.ts(b)]
        assert at_b and all(prev == b for prev, _ in at_b), b  # only in the step that starts at b
    for prev, as_of, _, _, r in recs:
        assert (prev is None or _at(r) >= feeds.ts(prev)) and _at(r) < feeds.ts(as_of)
    ids = [str(r.get("event_id") or r["settlement_id"]) for *_, r in recs]
    _, whole = staged(tmp_path / "whole", cfg())
    assert len(ids) == len(set(ids)) == sum(len(_records(whole)[k]) for k in ("clickstream", "source_feeds"))


# Seed 11 (3 days, 6,000 browsing events): evt_4010 is at exactly an interior midnight (found by search).
MIDNIGHT = g.World(seed=11, clock_start=datetime(2026, 1, 1, tzinfo=UTC), clock_end=datetime(2026, 1, 4, tzinfo=UTC),
                   sellers=10, customers=200, products=100, orders=300, clickstream_events=6000)


def test_day_aligned_steps_write_each_day_partition_from_one_step(tmp_path: Path) -> None:
    """Steps ending at midnight cover [D 00:00, D+1 00:00), exactly the date=D partition: the midnight event lands in
    its own day, in the step that starts at it, and no partition holds files of two steps (so replay keeps days
    whole). Under the old (previous, as_of] windows evt_4010 went to the previous day's step."""
    stops = [MIDNIGHT.clock_start + timedelta(days=d) for d in (1, 2, 3)]
    recs = _step_records(MIDNIGHT, cfg(), tmp_path, stops)
    owner: dict[str, set[str]] = {}
    for prev, _, date, run, r in recs:
        owner.setdefault(date, set()).add(run)
        assert date == _at(r)[:10] == (prev or MIDNIGHT.clock_start).date().isoformat()
    assert all(len(runs) == 1 for runs in owner.values()) and len(owner) == 3
    (hit,) = [(prev, date) for prev, _, date, _, r in recs if r.get("event_id") == "evt_4010"]
    assert hit[0] is not None and feeds.ts(hit[0]).endswith("T00:00:00Z") and hit[1] == hit[0].date().isoformat()


def _browse(world: g.World) -> list[dict[str, object]]:
    return [e for s in range(1, feeds.session_count(world) + 1) for e in feeds.session_events(world, s)]


@pytest.mark.parametrize("world", [WORLD, replace(WORLD, clock_end=WORLD.clock_start + timedelta(seconds=900)),
                                   replace(WORLD, clickstream_events=1), replace(WORLD, clickstream_events=0)])
def test_session_starts_never_decrease_and_bound_their_events(world: g.World) -> None:
    starts = [feeds.session_start(world, s) for s in range(1, feeds.session_count(world) + 1)]
    assert starts == sorted(starts)
    for e in _browse(world):
        start = starts[int(str(e["session_id"])[4:]) - 1]
        assert feeds.ts(start) <= str(e["event_time"]) <= feeds.ts(start + feeds.SESSION_SPAN)
        assert str(e["event_time"]) < feeds.ts(world.clock_end)
    for t in [world.clock_start - timedelta(seconds=1), world.clock_start, world.clock_end, *starts[:50:7]]:
        assert feeds.sessions_by(world, t) == sum(s < t for s in starts)
    with pytest.raises(feeds.FeedError, match="out of range"):
        feeds.session_start(world, feeds.session_count(world) + 1)


def test_a_step_generates_every_session_it_needs_and_no_older_ones() -> None:
    events = _browse(WORLD)
    times = sorted({str(e["event_time"]) for e in events})
    previous = None
    # steps ending exactly on event times (boundaries), one second after, empty and day-long windows
    stops = [datetime.fromisoformat(times[k]) for k in (0, 1, 500, 1200)]
    stops += [stops[-1] + timedelta(seconds=1), stops[-1] + timedelta(seconds=2), WORLD.clock_end]
    for as_of in stops:
        plan = g.plan_step(WORLD, previous, as_of)
        sessions = feeds.step_sessions(WORLD, plan)
        needed = {int(str(e["session_id"])[4:]) for e in events if feeds.in_window(plan, datetime.fromisoformat(str(e["event_time"])))}
        assert needed <= set(sessions)
        if previous is not None:  # nothing that ended before the window is regenerated
            assert feeds.session_start(WORLD, sessions.start) >= previous - feeds.SESSION_SPAN
        if sessions:
            assert feeds.session_start(WORLD, sessions.stop - 1) < as_of
        got = [e for b in feeds._step_sessions(WORLD, 37, plan) for e in b]
        assert sorted(str(e["event_id"]) for e in got) == sorted(
            str(e["event_id"]) for e in events if feeds.in_window(plan, datetime.fromisoformat(str(e["event_time"]))))
        previous = as_of


def test_an_empty_window_publishes_no_browsing_events(tmp_path: Path) -> None:
    sparse = replace(WORLD, clickstream_events=10)  # 2 sessions in 14 days: most windows hold none of them
    t = sparse.clock_start + timedelta(days=13, hours=23)
    plan = g.plan_step(sparse, t, t + timedelta(seconds=1))
    assert not [e for b in feeds._step_sessions(sparse, 250, plan) for e in b]
    stats = feeds.write_staged(sparse, cfg(), RUN, tmp_path, plan)
    assert not [e for rel, st in stats.items() if st.feed == "clickstream"
                for e in feeds.iter_records(tmp_path / rel, "jsonl") if e["event_type"] != "order_placed"]


def test_a_late_step_over_a_large_history_generates_only_recent_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A month and 1M browsing events (200k sessions): an hour-long step generates under 0.5 % of them."""
    big = replace(WORLD, clock_end=WORLD.clock_start + timedelta(days=30), clickstream_events=1_000_000)
    calls: list[int] = []
    real = feeds.session_events

    def counted(w: g.World, s: int, *a: Any) -> list[dict[str, object]]:
        calls.append(s)
        return real(w, s, *a)
    monkeypatch.setattr(feeds, "session_events", counted)
    previous = big.clock_end - timedelta(hours=2)
    plan = g.plan_step(big, previous, previous + timedelta(hours=1))
    got = [e for b in feeds._step_sessions(big, 5000, plan) for e in b]
    assert got and all(feeds.in_window(plan, datetime.fromisoformat(str(e["event_time"]))) for e in got)
    assert 0 < len(calls) < feeds.session_count(big) // 200
    assert min(calls) > feeds.sessions_by(big, previous - feeds.SESSION_SPAN)
    calls.clear()
    count = feeds.browsing_events_by(big, plan.as_of, 5000)
    assert len(calls) < feeds.session_count(big) // 500 and 0 < count < big.clickstream_events
