"""cleanup-files rules without a database: the confirmation token, root refusals and path checks
(deletion with PostgreSQL: integration/test_cleanup_files.py)."""

from pathlib import Path

import pytest

from ecomgen import cleanup, feeds

C = [cleanup.Candidate("clickstream/date=2026-01-05/a.jsonl", "a" * 64, 10, "published_files"),
     cleanup.Candidate("clickstream/date=2026-01-06/b.jsonl", "b" * 64, 20, "published_files")]
RUN = cleanup.Target("run", "run_20260101T000000Z_abcdef")


def test_token_is_order_independent_and_covers_target_and_every_field() -> None:
    t = cleanup.token(RUN, C)
    assert len(t) == 12 and t == cleanup.token(RUN, list(reversed(C)))
    variants = [C[:1], [C[0], cleanup.Candidate(C[1].path, "c" * 64, 20, C[1].ledger)],
                [C[0], cleanup.Candidate(C[1].path, C[1].sha256, 21, C[1].ledger)]]
    assert t not in {cleanup.token(RUN, v) for v in variants}
    assert t != cleanup.token(cleanup.Target("run", "run_20260101T000000Z_000000"), C)
    assert t != cleanup.token(cleanup.Target("replay", "rpl_000000000000"), C)


def test_target_shape() -> None:
    for bad in (("staging", "x"), ("run", None), ("everything", None)):
        with pytest.raises(cleanup.CleanupError):
            cleanup.Target(*bad)


@pytest.mark.parametrize("marker", [".git", "PG_VERSION"])
def test_roots_that_look_like_something_else_are_refused(tmp_path: Path, marker: str) -> None:
    cleanup.check_root(tmp_path)
    (tmp_path / marker).mkdir() if marker == ".git" else (tmp_path / marker).write_text("18\n")
    with pytest.raises(cleanup.CleanupError, match=marker):
        cleanup.check_root(tmp_path)


def test_filesystem_root_home_and_missing_root_are_refused(tmp_path: Path) -> None:
    for root in (Path("/"), Path.home(), tmp_path / "missing"):
        with pytest.raises(cleanup.CleanupError):
            cleanup.check_root(root)


def test_paths_must_stay_inside_the_root_without_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    (root / "a" / "f.csv").write_text("x\n")
    assert cleanup._safe_file(root, "a/f.csv") == root / "a" / "f.csv"
    for rel in ("../outside.csv", "/etc/hosts", "a/../a/f.csv", "a//f.csv"):
        with pytest.raises(feeds.FeedError, match="unsafe"):
            cleanup._safe_file(root, rel)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "f.csv").write_text("victim\n")
    (root / "link").symlink_to(outside)
    with pytest.raises(feeds.FeedError, match="symlink"):
        cleanup._safe_file(root, "link/f.csv")
    (root / "a" / "g.csv").symlink_to(outside / "f.csv")
    with pytest.raises(feeds.FeedError, match="symlink"):
        cleanup._safe_file(root, "a/g.csv")
