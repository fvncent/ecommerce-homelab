"""Publishing file feeds with PostgreSQL: ledger, manifest, crash recovery, no overwrites, and the validate gate
(files agree with the database both ways). Fixtures in conftest.py (data_root: a per-test ECOMGEN_DATA_ROOT)."""

import hashlib
import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest

from ecomgen import config, feeds, generate, load, validate
from ecomgen.cli import main

ADMIN_URL = os.environ.get("ECOMGEN_TEST_DATABASE_URL", "")
EXAMPLE = Path(__file__).parents[2] / "config" / "generator.example.yaml"
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="ECOMGEN_TEST_DATABASE_URL not set")

Migrated = Callable[..., psycopg.Connection]

# generate in a child process that SIGKILLs itself while publishing: just before placing staged file number N
# (argv[3]); the database chunks are all committed by then, so the run is RUNNING with some files published.
CRASH_IN_PUBLISH = """
import os, signal, sys, psycopg
from ecomgen import config, feeds, load
real, placed = feeds._place, [0]
def crash(*args, **kwargs):
    placed[0] += 1
    if placed[0] == int(sys.argv[3]):
        os.kill(os.getpid(), signal.SIGKILL)
    return real(*args, **kwargs)
feeds._place = crash
with psycopg.connect(sys.argv[2], autocommit=True) as conn:
    load.run(conn, config.load(sys.argv[1]), "smoke")
"""


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    path = tmp_path / "generator.yaml"
    path.write_text(EXAMPLE.read_text().replace("chunk_rows: 5000", "chunk_rows: 300"))
    return path


@pytest.fixture
def small_cfg(cfg_path: Path) -> config.Config:
    return config.load(cfg_path)


def tree(root: Path) -> dict[str, str]:
    """Relative path -> sha256 of every published file (staging excluded)."""
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and "staging" not in p.relative_to(root).parts}


def failing(conn: psycopg.Connection, cfg: config.Config) -> set[str]:
    return {r.check.name for r in validate.validate(conn, cfg)[1] if not r.ok}


def ledger(conn: psycopg.Connection) -> dict[str, tuple[str, int]]:
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT rel_path, sha256, records FROM generator_meta.published_files").fetchall()}


@pytest.fixture
def published(migrated: Migrated, small_cfg: config.Config, data_root: Path) -> psycopg.Connection:
    c = migrated(small_cfg)
    result = load.run(c, small_cfg, "smoke")
    assert result.files.files == result.files.newly_published > 2
    return c


def test_run_publishes_files_manifest_and_ledger(published: psycopg.Connection, small_cfg: config.Config,
                                                 data_root: Path) -> None:
    files = tree(data_root)
    recorded = ledger(published)
    assert set(files) == set(recorded) and all(files[p] == recorded[p][0] for p in files)
    assert not any((data_root / "staging").iterdir())  # staging cleaned
    run_id = published.execute("SELECT run_id FROM generator_meta.generation_runs").fetchone()[0]  # type: ignore[index]
    doc = json.loads((data_root / feeds.manifest_path(run_id)).read_text())
    assert doc["run_id"] == run_id and doc["data_version"] == generate.DATA_VERSION
    assert {f["file_path"] for f in doc["files"]} == set(recorded) - {feeds.manifest_path(run_id)}
    assert all(f["anomaly_count"] == 0 and f["records_written"] == recorded[f["file_path"]][1] for f in doc["files"])
    settled = published.execute("SELECT count(*) FROM commerce.payment_attempts WHERE state = 'SETTLED'").fetchone()
    feed_records = {k: sum(f["records_written"] for f in doc["files"] if f["feed"] == k)
                    for k in ("payment_settlement", "clickstream")}
    assert feed_records == {"payment_settlement": settled[0],  # type: ignore[index]
                            "clickstream": load.PROFILE_COUNTS["smoke"]["clickstream_events"] + 1000}
    results = validate.validate(published, small_cfg)[1]
    assert [r.check.name for r in results if not r.ok] == []
    assert len(results) == len(validate.CHECKS) + 2 + len(validate.FEED_CHECKS)


def test_publish_again_is_a_no_op(published: psycopg.Connection, small_cfg: config.Config, data_root: Path) -> None:
    before, rows = tree(data_root), ledger(published)
    run_id = published.execute("SELECT run_id FROM generator_meta.generation_runs").fetchone()[0]  # type: ignore[index]
    world = load.world_for(small_cfg, "smoke")
    again = feeds.publish(published, small_cfg, run_id, world, data_root)
    assert again.newly_published == 0 and again.files == len(rows)
    assert tree(data_root) == before and ledger(published) == rows


def test_crash_between_rename_and_ledger_insert_is_adopted(published: psycopg.Connection, small_cfg: config.Config,
                                                           data_root: Path) -> None:
    before = tree(data_root)
    run_id = published.execute("SELECT run_id FROM generator_meta.generation_runs").fetchone()[0]  # type: ignore[index]
    victim = sorted(p for p in before if p.endswith(".csv"))[-1]
    # the state after a crash right after renaming the victim: renamed, not recorded, manifest not written yet
    mrel = feeds.manifest_path(run_id)
    published.execute("DELETE FROM generator_meta.published_files WHERE rel_path IN (%s, %s)", (victim, mrel))
    (data_root / mrel).unlink()
    again = feeds.publish(published, small_cfg, run_id, load.world_for(small_cfg, "smoke"), data_root)
    assert again.newly_published == 1  # only the manifest; the victim was adopted, not rewritten
    assert victim in ledger(published) and mrel in ledger(published)
    assert {p: h for p, h in tree(data_root).items() if p != mrel} == {p: h for p, h in before.items() if p != mrel}


def test_never_overwrites(published: psycopg.Connection, small_cfg: config.Config, data_root: Path) -> None:
    run_id = published.execute("SELECT run_id FROM generator_meta.generation_runs").fetchone()[0]  # type: ignore[index]
    world = load.world_for(small_cfg, "smoke")
    victim = data_root / sorted(p for p in tree(data_root) if p.endswith(".jsonl"))[0]
    original = victim.read_bytes()
    victim.write_bytes(original + b'{"tampered":true}\n')  # a recorded file changed after publishing
    with pytest.raises(feeds.FeedError, match="missing or changed"):
        feeds.publish(published, small_cfg, run_id, world, data_root)
    assert victim.read_bytes().endswith(b'{"tampered":true}\n')
    assert "files_match_ledger" in failing(published, small_cfg)
    victim.write_bytes(original)
    rel = str(victim.relative_to(data_root))
    published.execute("DELETE FROM generator_meta.published_files WHERE rel_path = %s", (rel,))
    with pytest.raises(feeds.FeedError, match="ledger and manifest .* list different files"):
        feeds.publish(published, small_cfg, run_id, world, data_root)  # manifest recorded: verify-only path
    mrel = feeds.manifest_path(run_id)
    published.execute("DELETE FROM generator_meta.published_files WHERE rel_path = %s", (mrel,))
    (data_root / mrel).unlink()
    victim.write_bytes(b"someone else's file\n")  # an unrecorded file in the way
    with pytest.raises(feeds.FeedError, match="refusing to overwrite"):
        feeds.publish(published, small_cfg, run_id, world, data_root)
    assert victim.read_bytes() == b"someone else's file\n"


def test_sigkill_while_publishing_then_resume(migrated: Migrated, new_db: Callable[[], str], cfg_path: Path,
                                              small_cfg: config.Config, data_root: Path, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    url = new_db()
    conn = migrated(small_cfg, url)
    proc = subprocess.run([sys.executable, "-c", CRASH_IN_PUBLISH, str(cfg_path), url, "4"], capture_output=True,
                          timeout=120)
    assert proc.returncode == -signal.SIGKILL, proc.stderr.decode()
    assert conn.execute("SELECT state FROM generator_meta.generation_runs").fetchone() == ("RUNNING",)
    assert len(ledger(conn)) == 3  # files 1-3 published, the 4th never placed
    staged = list((data_root / "staging").rglob("*.*"))
    assert staged  # the killed run's staging is left behind, but nothing in it is visible as published
    assert not set(tree(data_root)) - set(ledger(conn))
    assert "run_completed" in failing(conn, small_cfg)

    result = load.resume(conn, small_cfg)
    assert result is not None and result.chunks_written == 0 and result.files.newly_published == result.files.files - 3
    assert failing(conn, small_cfg) == set()
    assert load.resume(conn, small_cfg) is None

    # identical to an uninterrupted run (paths differ only by run id)
    clean_root = tmp_path / "clean"
    clean_root.mkdir()
    monkeypatch.setenv("ECOMGEN_DATA_ROOT", str(clean_root))
    clean = migrated(small_cfg)
    load.run(clean, small_cfg, "smoke")
    run_ids = [c.execute("SELECT run_id FROM generator_meta.generation_runs").fetchone()[0]  # type: ignore[index]
               for c in (conn, clean)]

    def by_content(root: Path, run_id: str) -> dict[str, str]:
        return {p.replace(run_id, "RUN"): h for p, h in tree(root).items() if not p.startswith("manifests/")}
    assert by_content(data_root, run_ids[0]) == by_content(clean_root, run_ids[1])


def test_resume_refuses_another_data_root(migrated: Migrated, small_cfg: config.Config, tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    c = migrated(small_cfg)
    real = feeds.publish

    def boom(*args: object, **kwargs: object) -> feeds.Published:
        raise OSError("disk full (simulated)")
    monkeypatch.setattr(feeds, "publish", boom)
    with pytest.raises(OSError):
        load.run(c, small_cfg, "smoke")
    monkeypatch.setattr(feeds, "publish", real)
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("ECOMGEN_DATA_ROOT", str(other))
    with pytest.raises(load.LoadError, match="data_root"):
        load.resume(c, small_cfg)


def test_missing_data_root_refused_before_any_write(migrated: Migrated, small_cfg: config.Config, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    c = migrated(small_cfg)
    monkeypatch.setenv("ECOMGEN_DATA_ROOT", str(tmp_path / "nope"))
    with pytest.raises(load.LoadError, match="does not exist"):
        load.run(c, small_cfg, "smoke")
    assert c.execute("SELECT count(*) FROM generator_meta.generation_runs").fetchone() == (0,)


@pytest.mark.parametrize(("csv", "jsonl"), [(True, False), (False, True)])
def test_single_feed_runs_validate_clean(migrated: Migrated, small_cfg: config.Config, data_root: Path, csv: bool,
                                         jsonl: bool) -> None:
    one = replace(small_cfg, filesystem=replace(small_cfg.filesystem, csv=csv, jsonl=jsonl))
    c = migrated(one)
    assert set(load.run(c, one, "smoke").files.records) == set(feeds.enabled(one))
    results = validate.validate(c, one)[1]
    assert [r.check.name for r in results if not r.ok] == []
    names = {r.check.name for r in results}
    assert ("settlements_match_payments" in names) == csv and ("orders_placed_once" in names) == jsonl


def test_missing_fields_and_unreadable_files_are_violations(published: psycopg.Connection, small_cfg: config.Config,
                                                            data_root: Path) -> None:
    rel = _first(data_root, ".csv")
    _rewrite(published, data_root, rel, lambda ls: ls[:-1] + [",".join(ls[-1].split(",")[:4]) + "\n"])
    assert "settlements_match_payments" in failing(published, small_cfg)  # NULL fee/net/settled_at are not a pass
    other = data_root / _first(data_root, ".jsonl")
    other.chmod(0)
    try:
        bad = {r.check.name: r for r in validate.validate(published, small_cfg)[1] if not r.ok}
        assert any("unreadable" in s for s in bad["files_match_ledger"].samples)
    finally:
        other.chmod(0o644)


def test_file_appearing_before_placement_is_not_overwritten(migrated: Migrated, small_cfg: config.Config,
                                                            data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = migrated(small_cfg)
    real_link = os.link

    def racing_link(src: str, dst: str) -> None:  # someone writes the target between the check and the link
        Path(dst).write_bytes(b"restored by hand\n")
        real_link(src, dst)
    monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(feeds.FeedError, match="appeared while placing it"):
        load.run(c, small_cfg, "smoke")
    victim = next(p for p in data_root.rglob("*-part-00000.*") if "staging" not in p.parts)
    assert victim.read_bytes() == b"restored by hand\n"


def test_feeds_off_runs_ignore_the_data_root(migrated: Migrated, small_cfg: config.Config, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    off = replace(small_cfg, filesystem=replace(small_cfg.filesystem, csv=False, jsonl=False))
    c = migrated(off)
    real = load.write_chunks

    def boom(*args: object, **kwargs: object) -> tuple[int, int]:
        raise OSError("simulated")
    monkeypatch.setattr(load, "write_chunks", boom)
    with pytest.raises(OSError):
        load.run(c, off, "smoke")
    monkeypatch.setattr(load, "write_chunks", real)
    monkeypatch.setenv("ECOMGEN_DATA_ROOT", str(tmp_path / "anywhere-else"))
    assert load.resume(c, off) is not None


def test_feeds_disabled_publishes_nothing(migrated: Migrated, small_cfg: config.Config, data_root: Path) -> None:
    off = replace(small_cfg, filesystem=replace(small_cfg.filesystem, csv=False, jsonl=False))
    c = migrated(off)
    assert load.run(c, off, "smoke").files == feeds.Published(0, 0, {})
    assert tree(data_root) == {} and failing(c, off) == set()


def _rewrite(conn: psycopg.Connection, root: Path, rel: str, edit: Callable[[list[str]], list[str]]) -> None:
    """Edit a published file and update its ledger row to match, so only the content checks can notice."""
    path = root / rel
    lines = edit(path.read_text().splitlines(keepends=True))
    path.write_text("".join(lines))
    records = len(lines) - (1 if rel.endswith(".csv") else 0)
    conn.execute("UPDATE generator_meta.published_files SET records = %s, bytes = %s, sha256 = %s WHERE rel_path = %s",
                 (records, path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest(), rel))


def _first(root: Path, suffix: str) -> str:
    return sorted(p for p in tree(root) if p.endswith(suffix))[0]


def _drop_order_placed(lines: list[str]) -> list[str]:
    i = next(k for k, line in enumerate(lines) if '"order_placed"' in line)
    return lines[:i] + lines[i + 1:]


# Each corruption must be caught by its check (other checks may fail too: e.g. a rewritten file no longer matches
# the manifest's checksum).
FEED_CORRUPTIONS: dict[str, Callable[[psycopg.Connection, Path], object]] = {
    "settlements_match_payments": lambda c, r: c.execute(
        "UPDATE commerce.payment_attempts SET updated_at = updated_at + interval '1 second'"
        " WHERE payment_id = (SELECT min(payment_id) FROM commerce.payment_attempts WHERE state = 'SETTLED')"),
    "settled_payments_settled_once": lambda c, r: _rewrite(c, r, _first(r, ".csv"), lambda ls: ls[:-1]),
    "clickstream_ids_exist": lambda c, r: _rewrite(c, r, _first(r, ".jsonl"), lambda ls: ls + [
        json.dumps({"event_id": "evt_x", "event_type": "product_viewed", "customer_id": "cus_999999",
                    "product_id": None, "payload": {}}) + "\n"]),
    "orders_placed_once": lambda c, r: _rewrite(c, r, _first(r, ".jsonl"), _drop_order_placed),
    "events_unique_and_complete": lambda c, r: _rewrite(c, r, _first(r, ".jsonl"), lambda ls: ls + ls[-1:]),
    "files_match_ledger": lambda c, r: (r / _first(r, ".jsonl")).write_text("truncated\n"),
    "manifest_matches_ledger": lambda c, r: c.execute(
        "UPDATE generator_meta.published_files SET rel_path = rel_path || '.renamed'"
        " WHERE rel_path = (SELECT min(rel_path) FROM generator_meta.published_files WHERE feed = 'clickstream')"),
}


def test_every_file_check_has_a_corruption_test() -> None:
    assert set(FEED_CORRUPTIONS) == {c.name for c in validate.FEED_CHECKS} | {"files_match_ledger",
                                                                              "manifest_matches_ledger"}


@pytest.mark.parametrize("check", sorted(FEED_CORRUPTIONS))
def test_feed_corruption_is_caught(published: psycopg.Connection, small_cfg: config.Config, data_root: Path,
                                   check: str) -> None:
    FEED_CORRUPTIONS[check](published, data_root)
    assert check in failing(published, small_cfg)


def test_cli_generate_reports_files(db_url: str, monkeypatch: pytest.MonkeyPatch,
                                    capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("ECOMMERCE_DATABASE_URL", db_url)
    assert main(["init-db", str(EXAMPLE)]) == 0
    assert main(["check-config", str(EXAMPLE)]) == 0
    assert "files: payment_settlement, clickstream under" in capsys.readouterr().out
    assert main(["generate", str(EXAMPLE), "--profile", "smoke"]) == 0
    out = capsys.readouterr().out
    assert "newly published" in out and "clickstream=11000" in out
    monkeypatch.setenv("ECOMGEN_DATA_ROOT", "relative/path")
    assert main(["generate", str(EXAMPLE)]) == 2  # config error, before any run or file
    assert main(["status", str(EXAMPLE)]) == 0  # commands that write nothing don't care
