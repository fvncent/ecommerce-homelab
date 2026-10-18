"""cleanup-files rules without a database: the confirmation token, root refusals and path checks
(deletion with PostgreSQL: integration/test_cleanup_files.py)."""

import hashlib
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


def _file(root: Path, rel: str, data: bytes = b"x\n") -> cleanup.Candidate:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return cleanup.Candidate(rel, hashlib.sha256(data).hexdigest(), len(data), "published_files")


def test_unsafe_paths_and_symlinks_are_never_followed(tmp_path: Path) -> None:
    root, outside = tmp_path / "root", tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "f.csv").write_bytes(b"x\n")
    c = _file(root, "a/f.csv")
    assert cleanup._inspect(root, c) == "present"
    for rel in ("../elsewhere/f.csv", "/etc/hosts", "a/../a/f.csv", "a//f.csv"):
        with pytest.raises(feeds.FeedError, match="unsafe"):
            cleanup._delete(root, cleanup.Candidate(rel, c.sha256, 2, "published_files"))
    (root / "link").symlink_to(outside)  # a directory link on the way
    with pytest.raises(cleanup.CleanupError, match="symlink"):
        cleanup._delete(root, cleanup.Candidate("link/f.csv", c.sha256, 2, "published_files"))
    (root / "a" / "g.csv").symlink_to(outside / "f.csv")  # the file itself is a link
    with pytest.raises(cleanup.CleanupError, match="symlink"):
        cleanup._delete(root, cleanup.Candidate("a/g.csv", c.sha256, 2, "published_files"))
    assert (outside / "f.csv").read_bytes() == b"x\n"
    assert cleanup._delete(root, c) == "deleted" and not (root / "a" / "f.csv").exists()
    assert cleanup._delete(root, c) == "gone" and cleanup._inspect(root, c) == "gone"
    assert cleanup._inspect(root, cleanup.Candidate("missing/dir/f.csv", c.sha256, 2, "published_files")) == "gone"


def test_directory_swapped_for_a_symlink_mid_delete_cannot_redirect_it(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    root, outside = tmp_path / "root", tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "f.csv").write_bytes(b"x\n")  # same name and bytes: the attacker's target
    c = _file(root, "a/f.csv")
    real = cleanup._sha_at

    def swap_then_hash(dir_fd: int, name: str) -> str:  # while we hash, a/ becomes a link to elsewhere/
        (root / "a").rename(root / "a.moved")
        (root / "a").symlink_to(outside)
        return real(dir_fd, name)
    monkeypatch.setattr(cleanup, "_sha_at", swap_then_hash)
    assert cleanup._delete(root, c) == "deleted"
    assert (outside / "f.csv").read_bytes() == b"x\n"  # untouched: deletion went through the pinned directory
    assert not (root / "a.moved" / "f.csv").exists()


def test_changed_file_is_not_deleted(tmp_path: Path) -> None:
    root = tmp_path / "root"
    c = _file(root, "a/f.csv")
    (root / "a" / "f.csv").write_bytes(b"edited\n")
    assert cleanup._inspect(root, c) == "changed" and cleanup._delete(root, c) == "changed"
    assert (root / "a" / "f.csv").read_bytes() == b"edited\n"
