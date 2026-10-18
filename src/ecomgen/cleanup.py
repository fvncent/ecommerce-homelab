"""`ecomgen cleanup-files`: delete files the generator itself produced, only on explicit confirmation.
Contract: docs/FEEDS.md, "Cleanup".

Candidates come only from the database: a COMPLETED run's published feed files, a COMPLETED replay's delivered
copies, or the staging directories of known runs, replays and cleanups. Nothing is found by scanning, so a file the
generator didn't record can't become a candidate. Manifests, delivery manifests and audit reports never are.

A dry run (the default) inspects every candidate and prints the list and a token: a hash of the target and the exact
list. Deletion needs that token; a different token (or a changed state) refuses, with no side effects. A file whose
bytes no longer match its record is not a candidate (kept and reported). Ledger rows are never removed, only marked.

Deletion never follows a symlink: each file's directory is opened component by component from the data root with
O_NOFOLLOW, and the file is checked, hashed and unlinked through that directory descriptor, so swapping a directory
for a link at any moment can't redirect it outside the root.

Order (crash-safe): cleanups row (RUNNING, with its data root) → per item: delete, fsync the directory, then in one
transaction mark its ledger row / record it in details → audit report `audit/cleanup_id=<id>.json` (built only from
the database, placed without overwriting) → COMPLETED. A later --delete under the same root first finishes
interrupted cleanups' reports.
"""

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from ecomgen import config, db, feeds, load, replay
from ecomgen.config import Config

KINDS = ("staging", "replay", "run")
CLEANUP_ID = re.compile(r"cln_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{12}")
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class CleanupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    kind: str
    ident: str | None = None  # run_id / replay_id; None for staging

    def __post_init__(self) -> None:
        if self.kind not in KINDS or (self.kind == "staging") != (self.ident is None) or self.ident == "":
            raise CleanupError(f"bad cleanup target {self.kind} {self.ident!r}")

    def as_json(self) -> dict[str, object]:
        return {"kind": self.kind, "id": self.ident}


@dataclass(frozen=True)
class Candidate:
    path: str  # relative to the data root
    sha256: str | None  # recorded checksum; None for a staging directory
    bytes: int
    ledger: str | None  # "published_files" / "file_deliveries"; None for a staging directory
    gone: bool = False  # recorded but no longer on disk: deleting only marks it


@dataclass(frozen=True)
class Plan:
    target: Target
    root: Path
    candidates: list[Candidate]
    kept: list[str]  # what stays, and why
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
        f"{c.path} {c.sha256 or '-'} {c.bytes} {int(c.gone)}" for c in sorted(candidates, key=lambda c: c.path)]
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


# ---------- symlink-proof access ----------

def _open_parent(root: Path, rel: str) -> int | None:
    """A descriptor of rel's directory, opened from root one component at a time without following symlinks; None if
    a directory on the way doesn't exist. Raises CleanupError on a symlink (or non-directory) on the way."""
    parts = feeds.safe_rel(rel).split("/")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            try:
                nfd = os.open(part, _DIR, dir_fd=fd)
            except FileNotFoundError:
                os.close(fd)
                return None
            except OSError:  # ELOOP / ENOTDIR: a symlink or a file where a directory should be
                raise CleanupError(f"{rel}: {part} is a symlink or not a directory; not followed") from None
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _inspect(root: Path, c: Candidate) -> str:
    """'present' (a regular file with the recorded checksum), 'gone' or 'changed'. Never follows a symlink."""
    fd = _open_parent(root, c.path)
    if fd is None:
        return "gone"
    try:
        name = c.path.rsplit("/", 1)[-1]
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return "gone"
        if c.ledger is None:
            if not stat.S_ISDIR(st.st_mode):
                raise CleanupError(f"{c.path} is not a directory; not removed")
            return "present"
        if not stat.S_ISREG(st.st_mode):
            raise CleanupError(f"{c.path} is a symlink or not a regular file; not deleted")
        return "present" if _sha_at(fd, name) == c.sha256 else "changed"
    finally:
        os.close(fd)


def _sha_at(dir_fd: int, name: str) -> str:
    h = hashlib.sha256()
    f = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    with os.fdopen(f, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _delete(root: Path, c: Candidate) -> str:
    """Delete one candidate through its directory descriptor: 'deleted', 'gone' or 'changed'."""
    fd = _open_parent(root, c.path)
    if fd is None:
        return "gone"
    try:
        name = c.path.rsplit("/", 1)[-1]
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return "gone"
        if c.ledger is None:
            if not stat.S_ISDIR(st.st_mode):
                raise CleanupError(f"{c.path} is not a directory; not removed")
            shutil.rmtree(name, dir_fd=fd)  # fd-based: never follows links inside
        else:
            if not stat.S_ISREG(st.st_mode):
                raise CleanupError(f"{c.path} is a symlink or not a regular file; not deleted")
            if _sha_at(fd, name) != c.sha256:
                return "changed"
            if os.stat(name, dir_fd=fd, follow_symlinks=False).st_ino != st.st_ino:
                return "changed"  # replaced while we hashed it
            os.unlink(name, dir_fd=fd)
        os.fsync(fd)
        return "deleted"
    finally:
        os.close(fd)


# ---------- plan ----------

def _require_tables(conn: psycopg.Connection, cfg: Config) -> None:
    if not db.has_ledger(conn, cfg):
        raise db.RunLookupError("generator schema not found; run `ecomgen init-db` first")
    if not db.has_table(conn, cfg, "cleanups"):
        raise db.RunLookupError("cleanup tables not found; run `ecomgen init-db` to apply the pending migrations")


def plan(conn: psycopg.Connection, cfg: Config, target: Target) -> Plan:
    """What deleting this target would remove. Read-only (it hashes the candidates)."""
    _require_tables(conn, cfg)
    root = Path(config.data_root(cfg))
    check_root(root)
    candidates: list[Candidate] = []
    kept = [f"interrupted cleanup {cid}: its audit report is finished first on --delete" for (cid,) in conn.execute(
        db.q(cfg, "SELECT cleanup_id FROM {meta}.cleanups WHERE state = 'RUNNING' AND data_root = %s"
                  " ORDER BY started_at"), (str(root),))]
    if target.kind == "staging":
        known = {r[0] for r in conn.execute(db.q(cfg, "SELECT run_id FROM {meta}.generation_runs"
                                                      " UNION SELECT replay_id FROM {meta}.replays"
                                                      " UNION SELECT cleanup_id FROM {meta}.cleanups"))}
        staging = root / "staging"
        feeds.no_links(root, staging)
        for entry in sorted(staging.iterdir()) if staging.is_dir() else []:
            ours = any(p.fullmatch(entry.name) for p in (feeds.RUN_ID, replay.REPLAY_ID, CLEANUP_ID))
            if entry.is_symlink() or not entry.is_dir():
                kept.append(f"staging/{entry.name} (not a directory: skipped)")
            elif not ours or entry.name not in known:
                kept.append(f"staging/{entry.name} (unknown to this database: skipped)")
            else:
                size = sum(p.lstat().st_size for p in entry.rglob("*") if p.is_file() and not p.is_symlink())
                candidates.append(Candidate(f"staging/{entry.name}", None, size, None))
        return Plan(target, root, candidates, kept, token(target, candidates))
    if target.kind == "run":
        row = conn.execute(db.q(cfg, "SELECT state, resume_fields->>'data_root' FROM {meta}.generation_runs"
                                     " WHERE run_id = %s"), (target.ident,)).fetchone()
        if row is None:
            raise db.RunLookupError(f"run {target.ident} not found")
        what = f"run {target.ident}"
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
        what = f"replay {target.ident}"
        rows = conn.execute(db.q(cfg, "SELECT delivered_path, sha256, bytes, original_path IS NULL"
                                      " FROM {meta}.file_deliveries WHERE replay_id = %s AND deleted_at IS NULL"
                                      " ORDER BY delivered_path"), (target.ident,)).fetchall()
        ledger = "file_deliveries"
    if row[0] != "COMPLETED":
        raise CleanupError(f"{what} is {row[0]}; finish it first (resume / repeat the replay), then clean up")
    if row[1] is None or Path(row[1]) != root:
        raise CleanupError(f"{what} published under {row[1]}, not the current data root {root}")
    for path, sha, size, is_manifest in rows:
        if is_manifest:
            kept.append(f"{path} (manifest: kept)")
            continue
        c = Candidate(path, sha, int(size), ledger)
        state = _inspect(root, c)
        if state == "changed":
            kept.append(f"{path} (changed since it was recorded: not ours to delete, kept)")
        else:
            candidates.append(Candidate(path, sha, int(size), ledger, gone=state == "gone"))
    return Plan(target, root, candidates, kept, token(target, candidates))


# ---------- delete ----------

def _note(conn: psycopg.Connection, cfg: Config, cid: str, key: str, path: str) -> None:
    conn.execute(db.q(cfg, "UPDATE {meta}.cleanups SET details = jsonb_set(details, %s,"
                           " coalesce(details->%s, '[]') || to_jsonb(%s::text)) WHERE cleanup_id = %s"),
                 ([key], key, path, cid))


def _finish_audit(conn: psycopg.Connection, cfg: Config, root: Path, cleanup_id: str) -> str:
    """Write (or adopt) a cleanup's audit report from the database alone, then mark it COMPLETED."""
    row = conn.execute(db.q(cfg, "SELECT target, token, details, started_at FROM {meta}.cleanups"
                                 " WHERE cleanup_id = %s"), (cleanup_id,)).fetchone()
    assert row is not None
    target, tok, details, started = row
    gone = set(details.get("already_gone", []))
    marked = conn.execute(db.q(cfg, """
        SELECT rel_path, sha256, bytes, 'published_files', deleted_at FROM {meta}.published_files
        WHERE deleted_by = %(id)s
        UNION ALL SELECT delivered_path, sha256, bytes, 'file_deliveries', deleted_at FROM {meta}.file_deliveries
        WHERE deleted_by = %(id)s ORDER BY 1"""), {"id": cleanup_id}).fetchall()
    deleted = [{"path": r[0], "sha256": r[1], "bytes": int(r[2]), "ledger": r[3], "deleted_at": feeds.ts(r[4])}
               for r in marked if r[0] not in gone]
    doc = {"cleanup_id": cleanup_id, "target": target, "token": tok, "data_root": str(root),
           "started_at": feeds.ts(started), "deleted_files": deleted,
           "deleted_bytes": sum(int(r[2]) for r in marked if r[0] not in gone),
           "already_gone": sorted(gone), "staging_dirs_removed": details.get("staging_dirs", []),
           "kept_changed": details.get("kept_changed", [])}
    rel = audit_path(cleanup_id)
    work = root / "staging" / cleanup_id
    feeds.no_links(root, work)
    staged = work / rel
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    feeds.place_file(root, rel, staged, feeds.sha256_of(staged), None)
    shutil.rmtree(work)
    conn.execute(db.q(cfg, "UPDATE {meta}.cleanups SET state = 'COMPLETED', finished_at = now()"
                           " WHERE cleanup_id = %s"), (cleanup_id,))
    return rel


def run(conn: psycopg.Connection, cfg: Config, target: Target, confirm: str | None) -> tuple[Plan, Result | None]:
    """Dry run when confirm is None; otherwise delete the planned items if confirm equals the plan's token (a
    mismatch refuses before any change). Holds the run lock (never overlaps generate, resume or a replay)."""
    if not conn.autocommit:
        raise CleanupError("cleanup needs an autocommit connection so each deletion is recorded on its own")
    with load._run_lock(conn):
        p = plan(conn, cfg, target)
        if confirm is None:
            return p, None
        if confirm != p.token:
            raise CleanupError(f"confirmation token {confirm!r} does not match the current plan ({p.token}); the "
                               "files changed since the dry run, or the token is for another target: run the dry "
                               "run again and review it")
        for (pending,) in conn.execute(db.q(cfg, "SELECT cleanup_id FROM {meta}.cleanups WHERE state = 'RUNNING'"
                                                 " AND data_root = %s ORDER BY started_at"), (str(p.root),)).fetchall():
            _finish_audit(conn, cfg, p.root, pending)  # an earlier cleanup was interrupted: finish its report
        if not p.candidates:
            return p, Result(None, 0, 0, [], 0, None)
        cid = f"cln_{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{secrets.token_hex(6)}"
        conn.execute(db.q(cfg, "INSERT INTO {meta}.cleanups (cleanup_id, target, token, data_root, state)"
                               " VALUES (%s, %s, %s, %s, 'RUNNING')"),
                     (cid, Jsonb(target.as_json()), p.token, str(p.root)))
        deleted = gone = size = 0
        changed: list[str] = []
        for c in p.candidates:
            outcome = _delete(p.root, c)
            with conn.transaction():
                if outcome == "changed":
                    changed.append(c.path)
                    _note(conn, cfg, cid, "kept_changed", c.path)
                    continue
                if c.ledger is None:
                    if outcome == "deleted":
                        _note(conn, cfg, cid, "staging_dirs", c.path)
                        size += c.bytes
                    continue
                if outcome == "gone":
                    gone += 1
                    _note(conn, cfg, cid, "already_gone", c.path)
                else:
                    deleted += 1
                    size += c.bytes
                scope, key = ("run_id", "rel_path") if c.ledger == "published_files" else ("replay_id",
                                                                                           "delivered_path")
                conn.execute(db.q(cfg, f"UPDATE {{meta}}.{c.ledger} SET deleted_at = now(), deleted_by = %s"
                                       f" WHERE {scope} = %s AND {key} = %s AND deleted_at IS NULL"),
                             (cid, target.ident, c.path))
        rel = _finish_audit(conn, cfg, p.root, cid)
        return p, Result(cid, deleted, gone, changed, size, rel)
