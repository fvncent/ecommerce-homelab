"""Safety guards of scripts/homelab_bench.py; every refusal happens before any database connection."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "homelab_bench.py"
UNREACHABLE = "postgresql://u:p@127.0.0.1:1/{}?connect_timeout=1"  # a guard that connected first would hang/fail


@pytest.fixture
def bench(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("ECOMGEN_TEST_DATABASE_URL", UNREACHABLE.format("postgres"))
    monkeypatch.delenv("ECOMMERCE_DATABASE_URL", raising=False)
    spec = importlib.util.spec_from_file_location("homelab_bench", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["ecommerce_oltp", "postgres", "template1", "ecommerce_mvp", "ecommerce",
                                  "ecommerce_homelab_testx", "Ecommerce_homelab_test", ""])
def test_create_and_drop_refuse_names_outside_namespace(bench: ModuleType, name: str) -> None:
    for command in (bench.create, bench.drop, bench.db_url):
        with pytest.raises(bench.Refused, match="outside the benchmark namespace"):
            command(name)


def test_kept_benchmark_db_is_not_droppable(bench: ModuleType) -> None:
    assert bench.check_name("ecommerce_homelab_test") == "ecommerce_homelab_test"  # may be created
    with pytest.raises(bench.Refused, match="outside the benchmark namespace"):
        bench.drop("ecommerce_homelab_test")


@pytest.mark.parametrize("url", [
    "postgresql://u:p@h/ecommerce_homelab_test_x?dbname=ecommerce_oltp",
    "postgresql://u:p@h/ecommerce%5Foltp",
    "postgresql://u:p@h/x?sslmode=disable&dbname=ecommerce_oltp",
])
def test_urls_that_hide_the_real_database_are_refused(bench: ModuleType, url: str,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(bench.Refused, match="only in the path"):
        bench.dbname_of(url)
    monkeypatch.setenv("ECOMGEN_TEST_DATABASE_URL", url)
    with pytest.raises(bench.Refused, match="only in the path"):
        bench.db_url("ecommerce_homelab_test")


@pytest.mark.parametrize("target", ["ecommerce_oltp", "ecommerce_homelab_test", "anything"])
def test_crash_only_targets_a_sibling(bench: ModuleType, target: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ECOMMERCE_DATABASE_URL", UNREACHABLE.format(target))
    with pytest.raises(bench.Refused, match="outside the benchmark namespace"):
        bench.crash("config.yaml", "orders", "1")


def test_urls_come_from_env_not_argv(bench: ModuleType) -> None:
    with pytest.raises(bench.Refused, match="ECOMMERCE_DATABASE_URL is not set"):
        bench.crash("config.yaml", "orders", "1")
    with pytest.raises(bench.Refused, match="EXISTING_PG_URL is not set"):
        bench.pg_stats()
