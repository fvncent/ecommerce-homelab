"""External file feeds, derived from the same World as the PostgreSQL rows, so business IDs agree by construction.

Feeds (schema_version 1), partitioned by event date (UTC) under the data root:
- payment_settlement (CSV, `source_feeds/payment_settlement/date=D/{run}-{provider}-part-NNNNN.csv`): one row per
  payment attempt SETTLED by clock_end, in daily batches per provider; fee and net are exact Decimal amounts.
- clickstream (JSONL, `clickstream/date=D/{run}-part-NNNNN.jsonl`): World.clickstream_events browsing events in
  sessions of SESSION_EVENTS (`evt_N`, `ses_N`), plus one `order_placed` event per order (`evt_ord_N`).
Rows within a file follow generation order, not event time. Files roll over at filesystem.max_file_size_mb.
A clock step (`advance`) publishes the events of its window (previous_as_of, as_of] only (the first step:
[clock_start, as_of]): settlements whose settled_at is in it, order_placed events of the orders placed in it, and
browsing events whose event_time is in it. All steps of a chain together hold exactly the one-shot run's records.

Publishing (DB and filesystem writes are not atomic together, so this converges instead):
1. Every file of the run is regenerated into `staging/{run}/` (our own incomplete output, cleared first).
2. Each staged file is fsynced and hard-linked into place on the same filesystem (a link fails instead of replacing
   a file that appeared meanwhile), its directory chain fsynced, then recorded in {meta}.published_files; the run
   manifest (`manifests/run_id={run}/manifest.json`) goes last. Once the manifest is recorded, a repeat only
   verifies checksums.
3. A published file is never overwritten. On resume, a recorded file must still match its checksum, and an
   unrecorded file already at a target path (crash between rename and ledger insert) is adopted only if its bytes
   equal the regenerated ones. Anything else stops the run for a human to reconcile.
"""

import csv
import functools
import hashlib
import io
import json
import os
import re
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import psycopg

from ecomgen import __version__, db, generate
from ecomgen.config import Config

SCHEMA_VERSION = 1
SESSION_EVENTS = 5
SESSION_STEP = (5, 300)  # seconds between events of a session
SESSION_SPAN = timedelta(seconds=SESSION_EVENTS * SESSION_STEP[1])
ARRIVAL_DELAY = (1, 30)  # seconds from event_time to arrived_at
ANONYMOUS_PCT = 30
CACHE_ENTITIES = 50_000  # per kind; bounds memory (a few MB) for any catalogue size
# (percentage, fixed fee) per simulated provider
FEES = {"payco-sim": (Decimal("2.9"), Decimal("0.30")), "stripe-sim": (Decimal("2.9"), Decimal("0.30")),
        "adyen-sim": (Decimal("2.5"), Decimal("0.25"))}
BROWSE_TYPES = (("product_viewed", 60), ("search", 15), ("added_to_cart", 15), ("removed_from_cart", 5),
                ("wishlist_added", 5))
SETTLEMENT_COLUMNS = ("settlement_id", "batch_id", "payment_id", "order_id", "provider", "method", "currency",
                      "gross_amount", "fee_amount", "net_amount", "settled_at", "settlement_date")
SOURCE_SYSTEMS = {"payment_settlement": "payment_processor", "clickstream": "web_app"}
RUN_ID = re.compile(r"run_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{6}")


class FeedError(RuntimeError):
    pass


def ts(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- feed rows (pure) ----------

def settlement_row(p: generate.PaymentAttempt) -> dict[str, str] | None:
    """The settlement CSV row for a payment attempt, or None unless it reached SETTLED."""
    if p.state != "SETTLED":
        return None
    pct, fixed = FEES[p.provider]
    fee = min(p.amount, generate._money(p.amount * pct / 100 + fixed))
    day = p.updated_at.astimezone(UTC).date().isoformat()
    return {"settlement_id": "stl_" + p.payment_id.removeprefix("pay_"), "batch_id": f"{p.provider}-{day}",
            "payment_id": p.payment_id, "order_id": p.order_id, "provider": p.provider, "method": p.method,
            "currency": p.currency, "gross_amount": str(p.amount), "fee_amount": str(fee),
            "net_amount": str(p.amount - fee), "settled_at": ts(p.updated_at), "settlement_date": day}


def _event(event_id: str, event_type: str, customer_id: str | None, session_id: str, product_id: str | None,
           at: datetime, arrived: datetime, payload: dict[str, object]) -> dict[str, object]:
    return {"event_id": event_id, "schema_version": SCHEMA_VERSION, "event_type": event_type,
            "customer_id": customer_id, "session_id": session_id, "product_id": product_id,
            "event_time": ts(at), "arrived_at": ts(arrived), "source_system": SOURCE_SYSTEMS["clickstream"],
            "payload": payload}


def session_count(world: generate.World) -> int:
    return -(-world.clickstream_events // SESSION_EVENTS)


def session_events(world: generate.World, s: int,
                   customer: Callable[[int], generate.Customer] | None = None,
                   product: Callable[[int], generate.Product] | None = None) -> list[dict[str, object]]:
    """Browsing session s (1-based): events evt_{(s-1)*SESSION_EVENTS+1}.. up to clickstream_events, all before
    clock_end, one customer (or anonymous) per session. customer/product: optional (cached) World constructors."""
    customer, product = customer or world.customer, product or world.product
    rng = generate._rng(world.seed, "sessions", s)
    anonymous = generate._int(rng, 1, 100) <= ANONYMOUS_PCT
    found = generate._probe(rng, world.customers, customer, lambda c: c.status == "ACTIVE", set())
    customer_id = None if anonymous or found is None else found[1].customer_id
    last_start = max(world.clock_start, world.clock_end - SESSION_SPAN)
    t = generate._between(rng, world.clock_start, last_start)
    latest = world.clock_end - timedelta(seconds=1)
    out = []
    for m in range((s - 1) * SESSION_EVENTS + 1, min(world.clickstream_events, s * SESSION_EVENTS) + 1):
        t = min(latest, t + timedelta(seconds=generate._int(rng, *SESSION_STEP)))
        kind = generate._pick(rng, BROWSE_TYPES)
        picked = generate._probe(rng, world.products, product, lambda p: p.active, set())
        payload: dict[str, object]
        if kind == "search" or picked is None:
            kind, product_id = "search", None
            payload = {"query": generate._choice(rng, generate.LEAF_CATEGORIES).name.lower(),
                       "results": generate._int(rng, 0, 48)}
        else:
            product_id = picked[1].product_id
            payload = ({"page": "product_detail"} if kind == "product_viewed" else
                       {"quantity": generate._int(rng, 1, 3)} if kind == "added_to_cart" else
                       {"quantity": 1} if kind == "removed_from_cart" else {})
        arrived = t + timedelta(seconds=generate._int(rng, *ARRIVAL_DELAY))
        out.append(_event(f"evt_{m}", kind, customer_id, f"ses_{s}", product_id, t, arrived, payload))
    return out


def order_event(world: generate.World, order: generate.Order) -> dict[str, object]:
    """The order_placed clickstream event of an order, at placed_at."""
    rng = generate._rng(world.seed, "order_events", int(order.order_id.removeprefix("ord_")))
    arrived = order.placed_at + timedelta(seconds=generate._int(rng, *ARRIVAL_DELAY))
    return _event(f"evt_{order.order_id}", "order_placed", order.customer_id, f"ses_{order.order_id}", None,
                  order.placed_at, arrived,
                  {"order_id": order.order_id, "currency": order.currency, "total": str(order.total)})


# ---------- staged writing ----------

@dataclass
class FileStat:
    feed: str
    fmt: str
    records: int = 0
    start: datetime | None = None
    end: datetime | None = None


@dataclass
class _Writer:
    """Appends records to date-partitioned files under staging, rolling over at max_bytes. Only per-file stats are
    kept in memory; rows are flushed per call, so memory is bounded by the caller's chunk."""
    staging: Path
    max_bytes: int
    files: dict[str, FileStat] = field(default_factory=dict)
    _part: dict[str, tuple[int, int]] = field(default_factory=dict)  # base -> (part number, bytes in it)

    def add(self, feed: str, base: str, fmt: str, header: str | None, records: list[tuple[str, datetime]]) -> None:
        part, size = self._part.get(base, (0, 0))
        f = None
        try:
            for line, at in records:
                rel = f"{base}-part-{part:05d}.{fmt}"
                if size >= self.max_bytes and rel in self.files:  # full: continue in the next part
                    part, size = part + 1, 0
                    rel = f"{base}-part-{part:05d}.{fmt}"
                    if f is not None:
                        f.close()
                        f = None
                if f is None:
                    path = self.staging / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    f = path.open("a", encoding="utf-8", newline="")
                    if rel not in self.files:
                        self.files[rel] = FileStat(feed, fmt)
                        if header is not None:
                            size += len(header.encode())
                            f.write(header)
                stat = self.files[rel]
                size += len(line.encode())
                f.write(line)
                stat.records += 1
                stat.start = at if stat.start is None else min(stat.start, at)
                stat.end = at if stat.end is None else max(stat.end, at)
        finally:
            if f is not None:
                f.close()
        self._part[base] = (part, size)


def _csv_line(values: list[str]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(values)
    return buf.getvalue()


def _json_line(event: dict[str, object]) -> str:
    return json.dumps(event, separators=(",", ":")) + "\n"


def enabled(cfg: Config) -> list[str]:
    return [f for f, on in (("payment_settlement", cfg.filesystem.csv), ("clickstream", cfg.filesystem.jsonl)) if on]


def in_window(plan: generate.StepPlan | None, at: datetime) -> bool:
    """True if a step's files hold an event at `at`: (previous_as_of, as_of], or everything for plan None."""
    return plan is None or ((plan.previous is None or at > plan.previous) and at <= plan.as_of)


def _sessions(world: generate.World, rows: int) -> Iterator[list[dict[str, object]]]:
    """All browsing events, a bounded batch of sessions at a time. Entities are pure functions of their number, so a
    bounded cache can't change output; it saves regenerating the same products (and their sellers) per event."""
    customer: Callable[[int], generate.Customer] = functools.lru_cache(maxsize=CACHE_ENTITIES)(world.customer)
    product: Callable[[int], generate.Product] = functools.lru_cache(maxsize=CACHE_ENTITIES)(world.product)
    per_chunk = max(1, rows // SESSION_EVENTS)
    for first in range(1, session_count(world) + 1, per_chunk):
        last = min(session_count(world), first + per_chunk - 1)
        yield [e for s in range(first, last + 1) for e in session_events(world, s, customer, product)]


def browsing_events_by(world: generate.World, as_of: datetime, rows: int) -> int:
    """How many browsing events happened by as_of (all of them at clock_end)."""
    if as_of >= world.clock_end:
        return world.clickstream_events
    return sum(1 for batch in _sessions(world, rows) for e in batch
               if datetime.fromisoformat(str(e["event_time"])) <= as_of)


def write_staged(world: generate.World, cfg: Config, run_id: str, staging: Path,
                 plan: generate.StepPlan | None = None) -> dict[str, FileStat]:
    """Write every enabled feed of the run (or of the clock step `plan`: its window only) into staging; return
    per-file stats keyed by relative path."""
    feeds = enabled(cfg)
    w = _Writer(staging, cfg.filesystem.max_file_size_mb * 1024 * 1024)
    rows = cfg.generator.chunk_rows
    for orders, payments in _order_batches(world, rows, plan):
        if "payment_settlement" in feeds:
            _add_settlements(w, run_id, payments)
        if "clickstream" in feeds:
            _add_events(w, run_id, [order_event(world, o) for o in orders])
    if "clickstream" in feeds:
        for batch in _sessions(world, rows):
            _add_events(w, run_id, [e for e in batch
                                    if in_window(plan, datetime.fromisoformat(str(e["event_time"])))])
    return w.files


def _order_batches(world: generate.World, rows: int, plan: generate.StepPlan | None
                   ) -> Iterator[tuple[list[generate.Order], list[generate.PaymentAttempt]]]:
    """(orders placed, payment attempts settled) in the run or step window, one chunk of orders at a time. A step
    looks at the orders it writes or revisits: every order that can settle in its window is among them."""
    if plan is None:
        for chunk_no in range(generate.chunk_count(world, "orders", rows)):
            tables = generate.generate_chunk(world, "orders", chunk_no, rows).tables
            yield (cast(list[generate.Order], tables["orders"]),
                   cast(list[generate.PaymentAttempt], tables["payment_attempts"]))
        return
    for first in range(plan.orders.start, plan.orders.stop, rows):
        orders, payments = [], []
        for n in range(first, min(plan.orders.stop, first + rows)):
            order, _, attempts = world.order(n, plan.as_of)
            if n > plan.known:  # placed in the window
                orders.append(order)
            payments += [p for p in attempts if p.state == "SETTLED" and in_window(plan, p.updated_at)]
        yield orders, payments


def _add_settlements(w: _Writer, run_id: str, payments: list[generate.PaymentAttempt]) -> None:
    header = _csv_line(list(SETTLEMENT_COLUMNS))
    by_base: dict[str, list[tuple[str, datetime]]] = {}
    for p in payments:
        row = settlement_row(p)
        if row:
            base = f"source_feeds/payment_settlement/date={row['settlement_date']}/{run_id}-{row['provider']}"
            by_base.setdefault(base, []).append((_csv_line([row[c] for c in SETTLEMENT_COLUMNS]), p.updated_at))
    for base, recs in sorted(by_base.items()):
        w.add("payment_settlement", base, "csv", header, recs)


def _add_events(w: _Writer, run_id: str, events: list[dict[str, object]]) -> None:
    by_base: dict[str, list[tuple[str, datetime]]] = {}
    for e in events:
        at = datetime.fromisoformat(str(e["event_time"]))
        by_base.setdefault(f"clickstream/date={at.date().isoformat()}/{run_id}", []).append((_json_line(e), at))
    for base, recs in sorted(by_base.items()):
        w.add("clickstream", base, "jsonl", None, recs)


# ---------- publishing ----------

@dataclass(frozen=True)
class Published:
    files: int  # feed files + manifest recorded for the run
    newly_published: int
    records: dict[str, int]  # feed -> records


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durable(path: Path) -> None:
    with path.open("rb+") as f:
        os.fsync(f.fileno())


def _mkdirs(root: Path, d: Path) -> None:
    """mkdir -p under root, fsyncing the parent of each new directory, so a new partition survives a power cut."""
    missing = []
    while not d.exists():
        missing.append(d)
        d = d.parent
    for m in reversed(missing):
        m.mkdir(exist_ok=True)
        _fsync_dir(m.parent)


def _sync_up(root: Path, d: Path) -> None:
    """fsync d and every directory above it up to root (used when we can't know which entries are new)."""
    while True:
        _fsync_dir(d)
        if d == root:
            return
        d = d.parent


def _ledger(conn: psycopg.Connection, cfg: Config, run_id: str) -> dict[str, tuple[str, int, str]]:
    rows = conn.execute(db.q(cfg, "SELECT rel_path, sha256, bytes, feed FROM {meta}.published_files"
                                  " WHERE run_id = %s"), (run_id,)).fetchall()
    return {r[0]: (r[1], int(r[2]), r[3]) for r in rows}


def safe_rel(rel: object) -> str:
    """rel if it is a plain relative POSIX path (no '', '.', '..' components, no backslash, not absolute)."""
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel or "\0" in rel or any(
            part in ("", ".", "..") for part in rel.split("/")):
        raise FeedError(f"unsafe relative path {rel!r}")
    return rel


def no_links(root: Path, path: Path) -> None:
    """Refuse if path, or any existing directory between root and it, is a symlink (root itself may be one)."""
    rel = path.relative_to(root)
    p = root
    for part in rel.parts:
        p = p / part
        if p.is_symlink():
            raise FeedError(f"{p} is a symlink; refusing to follow it under the data root")
        if not p.exists():
            return


def place_file(root: Path, rel: str, staged: Path, sha: str, recorded_sha: str | None) -> bool:
    """Put staged (whose sha256 is sha) at root/rel without ever overwriting; True if newly placed. recorded_sha:
    what a ledger already records for rel (then only verify), or None. Raises FeedError when a human must look.
    Never follows a symlink below root, at the target or in staging."""
    final = root / safe_rel(rel)
    no_links(root, final)
    if recorded_sha is not None:
        if not final.is_file() or sha256_of(final) != recorded_sha:
            raise FeedError(f"recorded file {final} is missing or changed since it was recorded; reconcile by hand")
        if recorded_sha != sha:
            raise FeedError(f"regenerated {rel} differs from the recorded file; the generator changed under the run")
        return False
    if final.exists():
        if not final.is_file() or sha256_of(final) != sha:
            raise FeedError(f"{final} already exists with other content; refusing to overwrite it")
        # crash between placing and recording it: same bytes, adopt it, after making sure it is durable
        _durable(final)
        _sync_up(root, final.parent)
        return False
    no_links(root, staged)
    _durable(staged)
    _mkdirs(root, final.parent)
    try:
        os.link(staged, final)  # unlike rename, fails if a file appeared at final meanwhile
    except FileExistsError:
        raise FeedError(f"{final} appeared while placing it; refusing to overwrite it") from None
    _fsync_dir(final.parent)
    staged.unlink()
    return True


def _place(conn: psycopg.Connection, cfg: Config, run_id: str, root: Path, rel: str, staged: Path, stat: FileStat,
           ledger: dict[str, tuple[str, int, str]]) -> bool:
    """Publish one staged file and record it; True if it was newly published."""
    sha, size = sha256_of(staged), staged.stat().st_size
    recorded = ledger[rel][0] if rel in ledger else None
    newly = place_file(root, rel, staged, sha, recorded)
    if recorded is None:
        conn.execute(db.q(cfg, "INSERT INTO {meta}.published_files (run_id, rel_path, feed, format, schema_version,"
                               " records, bytes, sha256, start_event_time, end_event_time)"
                               " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"),
                     (run_id, rel, stat.feed, stat.fmt, SCHEMA_VERSION, stat.records, size, sha, stat.start, stat.end))
    return newly


def manifest(conn: psycopg.Connection, cfg: Config, run_id: str) -> dict[str, object]:
    """The run manifest, built only from the database (ledger + run), so rebuilding it yields the same bytes. A clock
    step's manifest also names the step: its window, and the chain's first step."""
    clock = "previous_as_of, as_of" if db.has_column(conn, cfg, "generation_runs", "as_of") else "NULL, NULL"
    run = conn.execute(db.q(cfg, "SELECT generator_version, config_sha256, seed, resume_fields->'data_version',"
                                 f" {clock}, resume_fields->'world'->>'clock_start'"
                                 " FROM {meta}.generation_runs WHERE run_id = %s"), (run_id,)).fetchone()
    assert run is not None
    files = conn.execute(db.q(cfg, "SELECT rel_path, feed, format, schema_version, start_event_time,"
                                   " end_event_time, records, bytes, sha256, published_at FROM {meta}.published_files"
                                   " WHERE run_id = %s AND feed <> 'manifest' ORDER BY rel_path"), (run_id,)).fetchall()
    step: dict[str, object] = {}
    if run[5] is not None:
        first = conn.execute(db.q(cfg, "SELECT run_id FROM {meta}.generation_runs WHERE previous_as_of IS NULL"
                                       " AND as_of IS NOT NULL")).fetchone()
        step = {"clock_step": {
            "first_step_run_id": first[0] if first else None, "previous_as_of": ts(run[4]) if run[4] else None,
            "as_of": ts(run[5]), "window": "(previous_as_of, as_of]" if run[4] else "[clock_start, as_of]",
            "window_start": ts(run[4]) if run[4] else ts(datetime.fromisoformat(run[6]))}}
    return {
        "run_id": run_id, "generator_version": run[0], "data_version": run[3], "config_sha256": run[1],
        "seed": run[2], **step, "files": [
            {"source_system": SOURCE_SYSTEMS[f[1]], "feed": f[1], "file_path": f[0], "format": f[2],
             "schema_version": f[3], "start_event_time": ts(f[4]) if f[4] else None,
             "end_event_time": ts(f[5]) if f[5] else None, "records_written": f[6], "bytes": f[7], "sha256": f[8],
             "anomaly_count": 0, "published_at": ts(f[9])} for f in files],
    }


def manifest_path(run_id: str) -> str:
    return f"manifests/run_id={run_id}/manifest.json"


def publish(conn: psycopg.Connection, cfg: Config, run_id: str, world: generate.World, root: Path,
            plan: generate.StepPlan | None = None) -> Published:
    """Stage, publish and record every file of the run (or clock step), then its manifest. Idempotent: safe to repeat
    after any crash; files already published (and unchanged) are left alone."""
    feeds = enabled(cfg)
    if not feeds:
        return Published(0, 0, {})
    if not RUN_ID.fullmatch(run_id):
        raise FeedError(f"unexpected run id {run_id!r}")
    if not root.is_dir():
        raise FeedError(f"data root {root} does not exist (create it, or set ECOMGEN_DATA_ROOT)")
    staging = root / "staging" / run_id
    if staging.exists():
        shutil.rmtree(staging)  # only ever our own unpublished output for this run
    ledger = _ledger(conn, cfg, run_id)
    if manifest_path(run_id) in ledger:  # everything was published (crash before COMPLETED): only verify
        return _verified(conn, cfg, run_id, root, ledger)
    stats = write_staged(world, cfg, run_id, staging, plan)
    unknown = set(ledger) - set(stats) - {manifest_path(run_id)}
    if unknown:
        raise FeedError(f"ledger records files this run would not produce: {sorted(unknown)[:3]}")
    newly = sum(_place(conn, cfg, run_id, root, rel, staging / rel, stats[rel], ledger) for rel in sorted(stats))
    rel = manifest_path(run_id)
    doc = manifest(conn, cfg, run_id)
    path = staging / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    files = cast(list[object], doc["files"])
    newly += _place(conn, cfg, run_id, root, rel, path, FileStat("manifest", "json", len(files)),
                    _ledger(conn, cfg, run_id))
    shutil.rmtree(staging)
    return replace(_verified(conn, cfg, run_id, root, _ledger(conn, cfg, run_id), check=False),
                   newly_published=newly)


def _verified(conn: psycopg.Connection, cfg: Config, run_id: str, root: Path,
              ledger: dict[str, tuple[str, int, str]], check: bool = True) -> Published:
    """Published counts from the ledger. With check: every recorded file still matches its checksum, and the
    published manifest lists exactly the recorded feed files."""
    for rel, (sha, _, _) in sorted(ledger.items()):
        if check and (not (root / rel).is_file() or sha256_of(root / rel) != sha):
            raise FeedError(f"recorded file {root / rel} is missing or changed since it was recorded; reconcile "
                            "by hand")
    if check:
        doc = json.loads((root / manifest_path(run_id)).read_text(encoding="utf-8"))
        if {f["file_path"] for f in doc["files"]} != set(ledger) - {manifest_path(run_id)}:
            raise FeedError(f"ledger and manifest of {run_id} list different files; reconcile by hand")
    rows = conn.execute(db.q(cfg, "SELECT feed, sum(records)::bigint FROM {meta}.published_files"
                                  " WHERE run_id = %s AND feed <> 'manifest' GROUP BY 1 ORDER BY 1"),
                        (run_id,)).fetchall()
    return Published(len(ledger), 0, {str(k): int(v) for k, v in rows})


def iter_records(path: Path, fmt: str) -> Iterator[dict[str, object]]:
    """Parse a published feed file row by row (CSV header included in each file)."""
    with path.open(encoding="utf-8", newline="") as f:
        if fmt == "csv":
            yield from csv.DictReader(f)
        else:
            for line in f:
                yield json.loads(line)
