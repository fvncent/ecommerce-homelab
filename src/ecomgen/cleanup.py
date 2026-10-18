"""`ecomgen cleanup-files`: delete files the generator itself produced, only on explicit confirmation.
Contract: docs/FEEDS.md, "Cleanup".

Candidates come only from the database: a COMPLETED run's published feed files, a COMPLETED replay's delivered
copies, or the staging directories of known runs and replays. Nothing is found by scanning, so a file the generator
didn't record can't become a candidate. Manifests, delivery manifests and audit reports are never candidates.

A dry run (the default) prints the candidates and a token: a hash of the target and the exact candidate list.
Deletion needs that token; a different token (or a changed state) refuses. Just before each deletion the path must
be a plain relative path inside the data root with no symlink on the way, a regular file, and still have its
recorded checksum; a changed file is kept and reported. Ledger rows are never removed, only marked.

Order (crash-safe): cleanups row (RUNNING, with the planned staging dirs) → per file: unlink, fsync the directory,
mark its ledger row → details (already gone, kept) → audit report `audit/cleanup_id=<id>.json` (built only from the
database, placed without overwriting) → COMPLETED. Any later cleanup first finishes interrupted cleanups' reports.
"""

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from ecomgen import config, db, feeds, load
from ecomgen.config import Config

KINDS = ("staging", "replay", "run")
_STAGING_NAME = re.compile(r"run_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{6}|rpl_[0-9a-f]{12}")


class CleanupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    kind: str
    ident: str | None = None  # run_id / replay_id; None for staging

    def __post_init__(self) -> None:
        if self.kind not in KINDS or (self.kind == "staging") != (self.ident is None):
            raise CleanupError(f"bad cleanup target {self.kind} {self.ident!r}")

    def as_json(self) -> dict[str, object]:
        return {"kind": self.kind, "id": self.ident}


@dataclass(frozen=True)
class Candidate:
    path: str  # relative to the data root
    sha256: str | None  # recorded checksum; None for a staging directory
    bytes: int
    ledger: str | None  # "published_files" / "file_deliveries"; None for a staging directory


@dataclass(frozen=True)
class Plan:
    target: Target
    root: Path
    candidates: list[Candidate]
    kept: list[str]  # manifests that stay, and why staging entries are skipped
    token: str


@dataclass(frozen=True)
class Result:
    cleanup_id: str | None  # None: nothing to delete
    deleted: int
    already_gone: int
    kept_changed: list[str]
    bytes: int
    audit_path: str | None


def token(target: Target, candidates: list[Candidate]) -> str:
    lines = [json.dumps(target.as_json(), sort_keys=True)] + [
        f"{c.path} {c.sha256 or '-'} {c.bytes}" for c in sorted(candidates, key=lambda c: c.path)]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:12]


def check_root(root: Path) -> None:
    """Refuse roots that are, or contain, something cleanup must never touch."""
    if not root.is_dir():
        raise CleanupError(f"data root {root} does not exist")
    if root.resolve() == Path("/") or root.resolve() == Path.home().resolve():
        raise CleanupError(f"refusing to clean up under {root}")
    for marker in (".git", "PG_VERSION"):
        if (root / marker).exists():
            raise CleanupError(f"data root {root} contains {marker}: it looks like a repository or a PostgreSQL "
                               "data directory, not a generator data root")


def audit_path(cleanup_id: str) -> str:
    return f"audit/cleanup_id={cleanup_id}.json"


def _dir_bytes(path: Path) -> int:
    return sum(p.lstat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def plan(conn: psycopg.Connection, cfg: Config, target: Target) -> Plan:
    """What deleting this target would remove (read-only)."""
    if not db.has_table(conn, cfg, "cleanups"):
        if not db.has_ledger(conn, cfg):
            raise db.RunLookupError("generator schema not found; run `ecomgen init-db` first")
        raise CleanupError("cleanup tables not found; run `ecomgen init-db` to apply the pending migrations")
    root = Path(config.data_root(cfg))
    check_root(root)
    candidates: list[Candidate] = []
    kept: list[str] = []
    if target.kind == "staging":
        known = {r[0] for r in conn.execute(db.q(cfg, "SELECT run_id FROM {meta}.generation_runs"
                                                      " UNION SELECT replay_id FROM {meta}.replays"))}
        staging = root / "staging"
        feeds.no_links(root, staging)
        for entry in sorted(staging.iterdir()) if staging.is_dir() else []:
            if entry.is_symlink() or not entry.is_dir():
                kept.append(f"staging/{entry.name} (not a directory: skipped)")
            elif not _STAGING_NAME.fullmatch(entry.name) or entry.name not in known:
                kept.append(f"staging/{entry.name} (unknown to this database: skipped)")
            else:
                candidates.append(Candidate(f"staging/{entry.name}", None, _dir_bytes(entry), None))
        return Plan(target, root, candidates, kept, token(target, candidates))
    if target.kind == "run":
        row = conn.execute(db.q(cfg, "SELECT state, resume_fields->>'data_root' FROM {meta}.generation_runs"
                                     " WHERE run_id = %s"), (target.ident,)).fetchone()
        if row is None:
            raise db.RunLookupError(f"run {target.ident} not found")
        what, recorded_root = f"run {target.ident}", row[1]
        rows = conn.execute(db.q(cfg, "SELECT rel_path, sha256, bytes, feed = 'manifest' FROM {meta}.published_files"
                                      " WHERE run_id = %s AND deleted_at IS NULL ORDER BY rel_path"),
                            (target.ident,)).fetchall()
        ledger = "published_files"
    else:
        row = conn.execute(db.q(cfg, "SELECT r.state, g.resume_fields->>'data_root' FROM {meta}.replays r"
                                     " JOIN {meta}.generation_runs g USING (run_id) WHERE r.replay_id = %s"),
                           (target.ident,)).fetchone()
        if row is None:
            raise db.RunLookupError(f"replay {target.ident} not found")
        what, recorded_root = f"replay {target.ident}", row[1]
        rows = conn.execute(db.q(cfg, "SELECT delivered_path, sha256, bytes, original_path IS NULL"
                                      " FROM {meta}.file_deliveries WHERE replay_id = %s AND deleted_at IS NULL"
                                      " ORDER BY delivered_path"), (target.ident,)).fetchall()
        ledger = "file_deliveries"
    if row[0] != "COMPLETED":
        raise CleanupError(f"{what} is {row[0]}; finish it first (resume / repeat the replay), then clean up")
    if recorded_root is None or Path(recorded_root) != root:
        raise CleanupError(f"{what} published under {recorded_root}, not the current data root {root}")
    for path, sha, size, is_manifest in rows:
        if is_manifest:
            kept.append(f"{path} (manifest: kept)")
        else:
            candidates.append(Candidate(path, sha, int(size), ledger))
    return Plan(target, root, candidates, kept, token(target, candidates))


def _finish_audit(conn: psycopg.Connection, cfg: Config, root: Path, cleanup_id: str) -> str:
    """Write (or adopt) the audit report of a cleanup from the database alone, then mark it COMPLETED."""
    row = conn.execute(db.q(cfg, "SELECT target, token, details, started_at FROM {meta}.cleanups"
                                 " WHERE cleanup_id = %s"), (cleanup_id,)).fetchone()
    assert row is not None
    deleted = [{"path": r[0], "sha256": r[1], "bytes": int(r[2]), "ledger": r[3], "deleted_at": feeds.ts(r[4])}
               for r in conn.execute(db.q(cfg, """
        SELECT rel_path, sha256, bytes, 'published_files', deleted_at FROM {meta}.published_files
        WHERE deleted_by = %(id)s
        UNION ALL SELECT delivered_path, sha256, bytes, 'file_deliveries', deleted_at FROM {meta}.file_deliveries
        WHERE deleted_by = %(id)s ORDER BY 1"""), {"id": cleanup_id})]
    doc = {"cleanup_id": cleanup_id, "target": row[0], "token": row[1], "started_at": feeds.ts(row[3]),
           "deleted_files": deleted, "deleted_bytes": sum(int(str(d["bytes"])) for d in deleted),
           "staging_dirs": row[2].get("staging_dirs", []), "already_gone": row[2].get("already_gone", []),
           "kept_changed": row[2].get("kept_changed", [])}
    rel = audit_path(cleanup_id)
    staged = root / "staging" / cleanup_id / rel
    feeds.no_links(root, staged)
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    feeds.place_file(root, rel, staged, feeds.sha256_of(staged), None)
    shutil.rmtree(root / "staging" / cleanup_id)
    conn.execute(db.q(cfg, "UPDATE {meta}.cleanups SET state = 'COMPLETED', finished_at = now()"
                           " WHERE cleanup_id = %s"), (cleanup_id,))
    return rel


def _safe_file(root: Path, rel: str) -> Path:
    path = root / feeds.safe_rel(rel)
    feeds.no_links(root, path)
    if root.resolve() not in path.resolve().parents:
        raise CleanupError(f"{rel} resolves outside the data root")
    return path


def run(conn: psycopg.Connection, cfg: Config, target: Target, confirm: str | None) -> tuple[Plan, Result | None]:
    """Dry run when confirm is None; otherwise delete the planned files if confirm equals the plan's token.
    Holds the run lock (never overlaps generate, resume or a replay)."""
    if not conn.autocommit:
        raise CleanupError("cleanup needs an autocommit connection so each deletion is recorded on its own")
    with load._run_lock(conn):
        p = plan(conn, cfg, target)
        if confirm is None:
            return p, None
        for (pending,) in conn.execute(db.q(cfg, "SELECT cleanup_id FROM {meta}.cleanups WHERE state = 'RUNNING'"
                                                 " ORDER BY started_at")).fetchall():
            _finish_audit(conn, cfg, p.root, pending)  # an earlier cleanup was interrupted: finish its report
        if confirm != p.token:
            raise CleanupError(f"confirmation token {confirm!r} does not match the current plan ({p.token}); the "
                               "files changed since the dry run, or the token is for another target: run the dry "
                               "run again and review it")
        if not p.candidates:
            return p, Result(None, 0, 0, [], 0, None)
        cid = f"cln_{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{p.token}"
        dirs = [c.path for c in p.candidates if c.ledger is None]
        conn.execute(db.q(cfg, "INSERT INTO {meta}.cleanups (cleanup_id, target, token, details, state)"
                               " VALUES (%s, %s, %s, %s, 'RUNNING')"),
                     (cid, Jsonb(target.as_json()), p.token, Jsonb({"staging_dirs": dirs})))
        deleted = size = 0
        gone: list[str] = []
        changed: list[str] = []
        for c in p.candidates:
            path = _safe_file(p.root, c.path)
            if c.ledger is None:  # a staging directory: only our own unpublished output
                if path.is_dir():
                    shutil.rmtree(path)
                    feeds._fsync_dir(path.parent)
                size += c.bytes
                continue
            if not path.exists():
                gone.append(c.path)
            elif not path.is_file() or feeds.sha256_of(path) != c.sha256:
                changed.append(c.path)
                continue
            else:
                path.unlink()
                feeds._fsync_dir(path.parent)
                deleted += 1
                size += c.bytes
            scope, key = ("run_id", "rel_path") if c.ledger == "published_files" else ("replay_id", "delivered_path")
            conn.execute(db.q(cfg, f"UPDATE {{meta}}.{c.ledger} SET deleted_at = now(), deleted_by = %s"
                                   f" WHERE {scope} = %s AND {key} = %s AND deleted_at IS NULL"),
                         (cid, target.ident, c.path))
        conn.execute(db.q(cfg, "UPDATE {meta}.cleanups SET details = details || %s WHERE cleanup_id = %s"),
                     (Jsonb({"already_gone": gone, "kept_changed": changed}), cid))
        rel = _finish_audit(conn, cfg, p.root, cid)
        return p, Result(cid, deleted, len(gone), changed, size, rel)
