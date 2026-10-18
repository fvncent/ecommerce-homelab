"""Replay schedule and identity without a database (delivery with PostgreSQL: integration/test_replay_files.py)."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from ecomgen import replay

RUN = "run_20260101T000000Z_abcdef"
P = replay.Params(date(2026, 1, 3), date(2026, 1, 9), ("clickstream", "payment_settlement"), 24, 12, 10)
ORIGINALS = [replay.Original(f"clickstream/date=2026-01-0{d}/{RUN}-part-0000{k}.jsonl", "clickstream",
                             date(2026, 1, d), f"{d}{k}".ljust(64, "0"), 100) for d in range(3, 10) for k in range(5)]


def test_replay_id_is_stable_and_covers_every_parameter() -> None:
    rid = replay.replay_id(RUN, P)
    assert rid == replay.replay_id(RUN, replace(P)) and rid.startswith("rpl_") and len(rid) == 16
    variants = [replace(P, start=date(2026, 1, 2)), replace(P, end=date(2026, 1, 10)),
                replace(P, feeds=("clickstream",)), replace(P, delay_hours=0), replace(P, jitter_hours=0),
                replace(P, duplicate_pct=0)]
    ids = {replay.replay_id(RUN, v) for v in variants} | {replay.replay_id("run_20260101T000000Z_000000", P)}
    assert rid not in ids and len(ids) == len(variants) + 1


def test_schedule_is_deterministic_sorted_and_bounded() -> None:
    rid = replay.replay_id(RUN, P)
    plan = replay.schedule(rid, ORIGINALS, P)
    assert plan == replay.schedule(rid, list(reversed(ORIGINALS)), P)
    assert plan == sorted(plan, key=lambda d: (d.arrived_at, d.path))
    for d in plan:
        day_end = datetime.combine(d.original.partition, datetime.min.time(), UTC) + timedelta(days=1)
        first = day_end + timedelta(hours=P.delay_hours)
        if not d.duplicate:
            assert first <= d.arrived_at < first + timedelta(hours=P.jitter_hours)
        assert d.path == (f"deliveries/replay_id={rid}/arrived={d.arrived_at.date().isoformat()}/"
                          + (d.original.path.replace(".jsonl", ".dup1.jsonl") if d.duplicate else d.original.path))
    assert len({d.path for d in plan}) == len(plan)
    originals = [d for d in plan if not d.duplicate]
    assert sorted(d.original.path for d in originals) == sorted(o.path for o in ORIGINALS)


def test_duplicates_follow_their_original_and_the_percentage() -> None:
    many = [replace(ORIGINALS[0], path=f"clickstream/date=2026-01-03/f{i}.jsonl") for i in range(2000)]
    for pct, lo, hi in ((0, 0, 0), (10, 150, 250), (100, 2000, 2000)):
        params = replace(P, duplicate_pct=pct)
        plan = replay.schedule(replay.replay_id(RUN, params), many, params)
        first = {d.original.path: d.arrived_at for d in plan if not d.duplicate}
        dups = [d for d in plan if d.duplicate]
        assert lo <= len(dups) <= hi
        assert all(timedelta(hours=1) <= d.arrived_at - first[d.original.path] < timedelta(hours=25) for d in dups)


def test_no_jitter_means_exact_delay() -> None:
    params = replace(P, jitter_hours=0, duplicate_pct=0)
    plan = replay.schedule(replay.replay_id(RUN, params), ORIGINALS[:1], params)
    assert plan[0].arrived_at == datetime(2026, 1, 5, tzinfo=UTC)  # end of Jan 3 + 24 h


@pytest.mark.parametrize("bad", [dict(start=date(2026, 1, 10)), dict(delay_hours=-1), dict(jitter_hours=-1),
                                 dict(duplicate_pct=101), dict(duplicate_pct=-1)])
def test_invalid_params(bad: dict[str, object]) -> None:
    with pytest.raises(replay.ReplayError):
        replace(P, **bad)  # type: ignore[arg-type]
