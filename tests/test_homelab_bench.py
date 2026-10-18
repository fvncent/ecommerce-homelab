"""Safety guards of scripts/homelab_bench.py; every refusal happens before any database connection."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "homelab_bench.py"
UNREACHABLE = "postgresql://u:p@127.0.0.1:1/{}?connect_timeout=1"  # a guard that connected first would hang/fail


@pytest.fixture
def bench(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("ECOMGEN_BENCH_URL", UNREACHABLE.format("ecommerce_homelab_test"))
    monkeypatch.delenv("ECOMMERCE_DATABASE_URL", raising=False)
    monkeypatch.delenv("EXISTING_PG_URL", raising=False)
    spec = importlib.util.spec_from_file_location("homelab_bench", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_only_the_three_approved_databases(bench: ModuleType) -> None:
    assert bench.DATABASES == ("ecommerce_homelab_test", "ecommerce_homelab_smoke", "ecommerce_homelab_crash")
    for name in bench.DATABASES:
        assert bench.db_url(name).endswith(f"/{name}?connect_timeout=1")


def test_no_create_or_drop_commands(bench: ModuleType) -> None:
    assert not {"create", "drop"} & set(bench.COMMANDS)
    assert not any(hasattr(bench, f) for f in ("create", "drop"))


@pytest.mark.parametrize("name", ["ecommerce_oltp", "postgres", "template1", "ecommerce_mvp", "ecommerce",
                                  "ecommerce_homelab_testx", "ecommerce_homelab_test_smoke", "Ecommerce_homelab_test",
                                  ""])
def test_names_outside_the_approved_set_are_refused(bench: ModuleType, name: str) -> None:
    for command in (bench.db_url, bench.sizes, bench.digest, bench.leftovers):
        with pytest.raises(bench.Refused, match="not an approved benchmark database"):
            command(name)


@pytest.mark.parametrize("base", ["postgres", "ecommerce_oltp", ""])
def test_base_url_must_name_an_approved_database(bench: ModuleType, base: str,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ECOMGEN_BENCH_URL", UNREACHABLE.format(base))
    for command in (bench.preflight, bench.wal_lsn, bench.server_major, lambda: bench.db_url("ecommerce_homelab_test")):
        with pytest.raises(bench.Refused, match="not an approved benchmark database"):
            command()


@pytest.mark.parametrize("url", [
    "postgresql://u:p@h/ecommerce_homelab_test?dbname=ecommerce_oltp",
    "postgresql://u:p@h/ecommerce%5Foltp",
    "postgresql://u:p@h/x?sslmode=disable&dbname=ecommerce_oltp",
])
def test_urls_that_hide_the_real_database_are_refused(bench: ModuleType, url: str,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(bench.Refused, match="only in the path"):
        bench.dbname_of(url)
    monkeypatch.setenv("ECOMGEN_BENCH_URL", url)
    with pytest.raises(bench.Refused, match="only in the path"):
        bench.db_url("ecommerce_homelab_test")
    monkeypatch.setenv("EXISTING_PG_URL", f"{UNREACHABLE.format('postgres')} {url}")
    with pytest.raises(bench.Refused, match="only in the path"):  # all checked before the first connection
        bench.pg_stats()


@pytest.mark.parametrize("target", ["ecommerce_oltp", "ecommerce_homelab_test", "ecommerce_homelab_smoke", "anything"])
def test_crash_only_targets_the_crash_database(bench: ModuleType, target: str,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ECOMMERCE_DATABASE_URL", UNREACHABLE.format(target))
    with pytest.raises(bench.Refused, match="not an approved benchmark database"):
        bench.crash("config.yaml", "orders", "1")


def test_urls_come_from_env_not_argv(bench: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(bench.Refused, match="ECOMMERCE_DATABASE_URL is not set"):
        bench.crash("config.yaml", "orders", "1")
    with pytest.raises(bench.Refused, match="EXISTING_PG_URL is not set"):
        bench.pg_stats()
    monkeypatch.delenv("ECOMGEN_BENCH_URL")
    with pytest.raises(bench.Refused, match="ECOMGEN_BENCH_URL is not set"):
        bench.preflight()
