"""Simulated historical delivery of a run's published files (`ecomgen replay-files`). Code-level contract:
docs/FEEDS.md, "Replay".

A replay copies the original files of a date range, byte for byte, to
`deliveries/replay_id=<id>/arrived=<D>/<original path>` (a duplicate delivery adds `.dupN` before the extension) and
writes `deliveries/replay_id=<id>/delivery_manifest.json` last. Originals are never touched. Everything is
deterministic: the replay id is a hash of the run and the parameters, and each file's arrival time and duplicate
choice come from a hash of the replay id and its original path. So the same command always produces the same
deliveries, and repeating it resumes an interrupted replay (or only verifies a finished one).

Before anything is delivered, the run's manifest must match its ledger checksum and every original in the range
must match the manifest; otherwise nothing is written. Delivered copies go through staging and feeds.place_file,
the same crash-safe, never-overwriting placement as publishing.
"""

import hashlib
import json
import random
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath

import psycopg
from psycopg.types.json import Jsonb

from ecomgen import config, db, feeds, load
from ecomgen.config import Config

REPLAY_VERSION = 1  # bump if the schedule or layout changes: it is part of the replay id
FEED_DIRS = {"payment_settlement": "source_feeds/payment_settlement/", "clickstream": "clickstream/"}
MAX_HOURS = 24 * 3650  # ten years: keeps every arrival time representable
REPLAY_ID = re.compile(r"rpl_[0-9a-f]{12}")
_PARTITION = re.compile(r"^[a-z_/]+/date=(\d{4}-\d{2}-\d{2})/[^/]+$")


class ReplayError(RuntimeError):
    pass


@dataclass(frozen=True)
class Params:
    start: date
    end: date
    feeds: tuple[str, ...]  # sorted
    delay_hours: int
    jitter_hours: int
    duplicate_pct: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "feeds", tuple(sorted(set(self.feeds))))  # one logical replay, one id
        if not self.feeds or not set(self.feeds) <= set(FEED_DIRS):
            raise ReplayError(f"feeds must be a non-empty subset of {', '.join(FEED_DIRS)}, got {self.feeds}")
        if self.end < self.start:
            raise ReplayError(f"--to {self.end} is before --from {self.start}")
        if not (0 <= self.delay_hours <= MAX_HOURS and 0 <= self.jitter_hours <= MAX_HOURS):
            raise ReplayError(f"delay and jitter must be 0..{MAX_HOURS} hours")
        if not 0 <= self.duplicate_pct <= 100:
            raise ReplayError("duplicate percentage must be in 0..100")

    def as_json(self) -> dict[str, object]:
        return {"from": self.start.isoformat(), "to": self.end.isoformat(), "feeds": list(self.feeds),
                "delay_hours": self.delay_hours, "jitter_hours": self.jitter_hours,
                "duplicate_pct": self.duplicate_pct, "replay_version": REPLAY_VERSION}


@dataclass(frozen=True)
class Original:
    path: str  # relative to the data root
    feed: str
    partition: date
    sha256: str
    bytes: int


@dataclass(frozen=True)
class Delivery:
    path: str  # delivered, relative to the data root
    original: Original
    arrived_at: datetime
    duplicate: bool


@dataclass(frozen=True)
class ReplayResult:
    replay_id: str
    run_id: str
    deliveries: int  # files delivered (the delivery manifest not counted)
    duplicates: int
    newly_delivered: int  # of deliveries, placed by this call
    verified_only: bool


def replay_id(run_id: str, params: Params) -> str:
    key = json.dumps({"run_id": run_id, **params.as_json()}, sort_keys=True)
    return "rpl_" + hashlib.sha256(key.encode()).hexdigest()[:12]


def _rng(rid: str, path: str) -> random.Random:
    return random.Random(int.from_bytes(hashlib.sha256(f"{rid}:{path}".encode()).digest()[:16], "big"))


def schedule(rid: str, sources: list[Original], params: Params) -> list[Delivery]:
    """Pure: when each original arrives, and which also arrive a second time. A partition's file becomes available
    at the end of its day, plus delay_hours, plus up to jitter_hours (whole seconds); a duplicate arrives 1-24 h
    after its first delivery. Sorted by arrival, then path."""
    out = []
    for o in sources:
        rng = _rng(rid, o.path)
        day_end = datetime(o.partition.year, o.partition.month, o.partition.day, tzinfo=UTC) + timedelta(days=1)
        arrived = day_end + timedelta(hours=params.delay_hours,
                                      seconds=int(rng.random() * params.jitter_hours * 3600))
        dup = int(rng.random() * 100) < params.duplicate_pct  # both draws always made: independent of the pct
        dup_at = arrived + timedelta(hours=1 + int(rng.random() * 24))
        out.append(Delivery(_delivered(rid, arrived, o.path, 0), o, arrived, False))
        if dup:
            out.append(Delivery(_delivered(rid, dup_at, o.path, 1), o, dup_at, True))
    return sorted(out, key=lambda d: (d.arrived_at, d.path))


def _delivered(rid: str, at: datetime, path: str, n: int) -> str:
    if n:  # name.ext -> name.dupN.ext (only the file name; directories untouched)
        p = PurePosixPath(path)
        stem, dot, ext = p.name.rpartition(".")
        path = str(p.with_name(f"{stem}.dup{n}.{ext}" if dot and stem else f"{p.name}.dup{n}"))
    return f"deliveries/replay_id={rid}/arrived={at.date().isoformat()}/{path}"


def delivery_manifest_path(rid: str) -> str:
    return f"deliveries/replay_id={rid}/delivery_manifest.json"


def delivery_manifest(rid: str, run_id: str, source_manifest_sha: str, params: Params,
                      plan: list[Delivery]) -> dict[str, object]:
    """Built only from the plan (no wall-clock times), so rebuilding it gives the same bytes."""
    return {"replay_id": rid, "run_id": run_id, "kind": "replayed_delivery", "params": params.as_json(),
            "source_manifest": {"file_path": feeds.manifest_path(run_id), "sha256": source_manifest_sha},
            "deliveries": [{"delivered_path": d.path, "original_path": d.original.path, "feed": d.original.feed,
                            "partition_date": d.original.partition.isoformat(), "sha256": d.original.sha256,
                            "bytes": d.original.bytes, "arrived_at": feeds.ts(d.arrived_at),
                            "duplicate": d.duplicate} for d in plan]}


def originals(conn: psycopg.Connection, cfg: Config, run_id: str, root: Path,
               params: Params) -> tuple[str, list[Original]]:
    """The run's manifest (checked against its ledger row) and its files in range, each checked against it.
    Raises with every problem before anything is written."""
    mrel = feeds.manifest_path(run_id)
    row = conn.execute(db.q(cfg, "SELECT sha256 FROM {meta}.published_files WHERE run_id = %s AND rel_path = %s"),
                       (run_id, mrel)).fetchone()
    if row is None:
        raise ReplayError(f"run {run_id} has no recorded manifest (it published no files)")
    mpath = root / mrel
    if not mpath.is_file() or feeds.sha256_of(mpath) != row[0]:
        raise ReplayError(f"manifest {mpath} is missing or differs from its recorded checksum")
    doc = json.loads(mpath.read_text(encoding="utf-8"))
    cleaned: dict[str, str] = {r[0]: r[1] for r in conn.execute(db.q(
        cfg, "SELECT rel_path, deleted_by FROM {meta}.published_files WHERE run_id = %s AND deleted_by IS NOT NULL"),
        (run_id,))} if db.has_table(conn, cfg, "cleanups") else {}  # before migration 0007 nothing was cleaned
    out, problems = [], []
    for f in doc["files"]:
        if f["feed"] not in params.feeds:
            continue
        rel = f["file_path"]
        try:
            m = _PARTITION.match(feeds.safe_rel(rel))
            if not m or not rel.startswith(FEED_DIRS[f["feed"]]):
                raise feeds.FeedError(f"unexpected path for a {f['feed']} file: {rel!r}")
            day = date.fromisoformat(m.group(1))
            path = root / rel
            feeds.no_links(root, path)
        except (feeds.FeedError, ValueError) as e:
            problems.append(f"{rel!r}: {e}")
            continue
        if not params.start <= day <= params.end:
            continue
        if rel in cleaned:
            problems.append(f"{rel}: deleted by cleanup {cleaned[rel]}")
        elif not path.is_file():
            problems.append(f"{f['file_path']}: missing")
        elif path.stat().st_size != f["bytes"] or feeds.sha256_of(path) != f["sha256"]:
            problems.append(f"{f['file_path']}: differs from the manifest")
        else:
            out.append(Original(f["file_path"], f["feed"], day, f["sha256"], f["bytes"]))
    if problems:
        raise ReplayError(f"{len(problems)} original file(s) can't be replayed; nothing was delivered by this "
                          "attempt: " + "; ".join(sorted(problems)[:5]))
    if not out:
        raise ReplayError(f"no {'/'.join(params.feeds)} files with partitions {params.start}..{params.end}")
    return row[0], sorted(out, key=lambda o: o.path)


def replay(conn: psycopg.Connection, cfg: Config, params: Params, run_id: str | None = None) -> ReplayResult:
    """Deliver (or finish delivering, or verify) the replay these parameters define. Holds the run lock, so it
    never overlaps a generate/resume, another replay or a cleanup."""
    if not conn.autocommit:
        raise ReplayError("replay needs an autocommit connection so each delivery commits on its own")
    if not db.has_ledger(conn, cfg):
        raise db.RunLookupError("generator schema not found; run `ecomgen init-db` first")
    if not db.has_table(conn, cfg, "replays"):
        raise ReplayError("replay tables not found; run `ecomgen init-db` to apply the pending migrations")
    with load._run_lock(conn):
        run_id = db.resolve_run(conn, cfg, run_id)
        row = conn.execute(db.q(cfg, "SELECT state, resume_fields FROM {meta}.generation_runs WHERE run_id = %s"),
                           (run_id,)).fetchone()
        assert row is not None
        state, recorded = row
        if state != "COMPLETED":
            raise ReplayError(f"run {run_id} is {state}; only a COMPLETED run's files can be replayed")
        recorded_root = (recorded or {}).get("data_root")
        root = Path(config.data_root(cfg))
        if recorded_root is None or Path(recorded_root) != root:
            raise ReplayError(f"run {run_id} published under {recorded_root}, not the current data root {root}")
        unknown = set(params.feeds) - set((recorded or {}).get("feeds", {}).get("enabled") or [])
        if unknown:
            raise ReplayError(f"run {run_id} did not publish {', '.join(sorted(unknown))}")
        rid = replay_id(run_id, params)
        staging = root / "staging" / rid
        feeds.no_links(root, staging)
        if staging.exists():
            shutil.rmtree(staging)  # our own unrecorded copies from an interrupted (or crashed-after-commit) attempt
        done: dict[str, str] = {r[0]: r[1] for r in conn.execute(db.q(
            cfg, "SELECT delivered_path, sha256 FROM {meta}.file_deliveries WHERE replay_id = %s"), (rid,))}
        mrel = delivery_manifest_path(rid)
        if mrel in done:  # finished earlier: verify the deliveries only (originals may since have been cleaned up)
            cleaned = conn.execute(db.q(cfg, "SELECT min(deleted_by), count(*) FROM {meta}.file_deliveries"
                                             " WHERE replay_id = %s AND deleted_by IS NOT NULL"),
                                   (rid,)).fetchone() if db.has_table(conn, cfg, "cleanups") else None
            if cleaned and cleaned[1]:
                raise ReplayError(f"replay {rid}: {cleaned[1]} delivered file(s) were removed by cleanup "
                                  f"{cleaned[0]}; it can't be verified (replay with other parameters for new copies)")
            for path, sha in sorted(done.items()):
                feeds.place_file(root, path, root / path, sha, sha)
            n, dups = conn.execute(db.q(cfg, "SELECT count(*), count(*) FILTER (WHERE duplicate)"
                                             " FROM {meta}.file_deliveries WHERE replay_id = %s"
                                             " AND original_path IS NOT NULL"), (rid,)).fetchone()  # type: ignore[misc]
            return ReplayResult(rid, run_id, int(n), int(dups), 0, True)
        source_sha, sources = originals(conn, cfg, run_id, root, params)
        conn.execute(db.q(cfg, "INSERT INTO {meta}.replays (replay_id, run_id, params, state)"
                               " VALUES (%s, %s, %s, 'RUNNING') ON CONFLICT (replay_id) DO NOTHING"),
                     (rid, run_id, Jsonb(params.as_json())))
        plan = schedule(rid, sources, params)
        unexpected = set(done) - {d.path for d in plan}
        if unexpected:
            raise ReplayError(f"replay {rid} recorded deliveries its plan doesn't contain: {sorted(unexpected)[:3]}")
        dups = sum(d.duplicate for d in plan)
        newly = 0
        for d in plan:
            staged = staging / d.path
            if d.path not in done:
                staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / d.original.path, staged)
                if feeds.sha256_of(staged) != d.original.sha256:
                    raise ReplayError(f"{d.original.path} changed while it was being copied")
            newly += feeds.place_file(root, d.path, staged, d.original.sha256, done.get(d.path))
            if d.path not in done:
                conn.execute(db.q(cfg, "INSERT INTO {meta}.file_deliveries (replay_id, run_id, delivered_path,"
                                       " original_path, sha256, bytes, arrived_at, duplicate)"
                                       " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"),
                             (rid, run_id, d.path, d.original.path, d.original.sha256, d.original.bytes,
                              d.arrived_at, d.duplicate))
        staged = staging / mrel
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(json.dumps(delivery_manifest(rid, run_id, source_sha, params, plan), indent=2) + "\n",
                          encoding="utf-8")
        sha = feeds.sha256_of(staged)
        size = staged.stat().st_size
        feeds.place_file(root, mrel, staged, sha, None)
        with conn.transaction():
            conn.execute(db.q(cfg, "INSERT INTO {meta}.file_deliveries (replay_id, run_id, delivered_path, sha256,"
                                   " bytes) VALUES (%s, %s, %s, %s, %s)"), (rid, run_id, mrel, sha, size))
            conn.execute(db.q(cfg, "UPDATE {meta}.replays SET state = 'COMPLETED', finished_at = now()"
                                   " WHERE replay_id = %s"), (rid,))
        shutil.rmtree(staging)
        return ReplayResult(rid, run_id, len(plan), dups, newly, False)
