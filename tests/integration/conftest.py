"""Fixtures for tests needing a real PostgreSQL (ECOMGEN_TEST_DATABASE_URL). Each test gets throwaway databases."""

import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest
from psycopg import sql

from ecomgen import config, db, load

ADMIN_URL = os.environ.get("ECOMGEN_TEST_DATABASE_URL", "")
EXAMPLE = Path(__file__).parents[2] / "config" / "generator.example.yaml"
# Test modules repeat ADMIN_URL/EXAMPLE and their own skipif marker; tests/ is not a package, so no imports.


@pytest.fixture(autouse=True)
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Feeds publish under a per-test directory (also inherited by subprocesses)."""
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("ECOMGEN_DATA_ROOT", str(root))
    return root


@pytest.fixture
def new_db() -> Iterator[Callable[[], str]]:
    """Factory: each call creates an empty database and returns its URL; all are dropped afterwards."""
    names: list[str] = []

    def create() -> str:
        name = f"ecomgen_test_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        names.append(name)
        return urlsplit(ADMIN_URL)._replace(path=f"/{name}").geturl()

    try:
        yield create
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            for name in names:
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def db_url(new_db: Callable[[], str]) -> str:
    return new_db()


@pytest.fixture
def cfg() -> config.Config:
    return config.load(EXAMPLE)


@pytest.fixture
def migrated(new_db: Callable[[], str]) -> Iterator[Callable[..., psycopg.Connection]]:
    """Factory: migrated(cfg, url=None) migrates url (default: a new database) and returns an autocommit connection."""
    conns: list[psycopg.Connection] = []

    def make(cfg: config.Config, url: str | None = None) -> psycopg.Connection:
        url = url or new_db()
        with psycopg.connect(url) as c:
            db.migrate(c, cfg)
        conns.append(psycopg.connect(url, autocommit=True))
        return conns[-1]

    try:
        yield make
    finally:
        for c in conns:
            c.close()


@pytest.fixture
def conn(db_url: str, cfg: config.Config) -> Iterator[psycopg.Connection]:
    with psycopg.connect(db_url) as c:
        db.migrate(c, cfg)
        yield c


Digest = Callable[[psycopg.Connection], dict[str, str]]


@pytest.fixture
def digest() -> Digest:
    """Content hash of every source table (rows sorted by their text form, so order is deterministic)."""
    def compute(conn: psycopg.Connection) -> dict[str, str]:
        out = {}
        for t in load.TABLES:
            row = conn.execute(f"SELECT md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), ''))"
                               f" FROM commerce.{t} x".encode()).fetchone()
            out[t] = str(row[0]) if row else ""
        return out
    return compute
