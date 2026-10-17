from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from ecomgen import config
from ecomgen.cli import main

EXAMPLE = Path(__file__).parent.parent / "config" / "generator.example.yaml"
URL = "postgresql://u:secret@localhost:5432/ecommerce_oltp"


def example() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(EXAMPLE.read_bytes())
    return data


def parse(data: dict[str, Any]) -> config.Config:
    return config.parse(yaml.safe_dump(data).encode())


def test_example_config_loads() -> None:
    cfg = config.load(EXAMPLE)
    assert cfg.generator.seed == 20261017
    assert cfg.generator.counts["orders"] == 10000
    assert cfg.generator.clock_start == datetime(2026, 1, 1, tzinfo=UTC)
    assert cfg.postgres.metadata_schema == "generator_meta"
    assert str(cfg.filesystem.root) == "/srv/ecommerce-data"
    assert len(cfg.sha256) == 64


def test_sha256_tracks_file_bytes() -> None:
    raw = EXAMPLE.read_bytes()
    assert config.parse(raw).sha256 == config.parse(raw).sha256
    assert config.parse(raw).sha256 != config.parse(raw + b"\n# changed\n").sha256


def test_non_utc_offset_is_normalized() -> None:
    d = example()
    d["generator"]["simulation"]["clock_start"] = "2026-01-01T02:00:00+02:00"
    assert parse(d).generator.clock_start == datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("section", "key", "value", "match"),
    [
        ("generator", "seed", -1, r"generator\.seed"),
        ("generator", "seed", True, r"generator\.seed"),
        ("generator", "seed", 2**63, r"generator\.seed"),
        ("generator", "workers", 3, r"generator\.workers"),
        ("generator", "chunk_rows", 0, r"generator\.chunk_rows"),
        ("generator", "profile", "huge", r"generator\.profile"),
        ("generator", "version", 2, r"generator\.version"),
        ("generator", "typo_key", 1, r"generator: unknown key\(s\): typo_key"),
        ("postgres", "application_schema", "Bad-Name", r"postgres\.application_schema"),
        ("postgres", "metadata_schema", "commerce", "distinct"),
        ("postgres", "metadata_schema", "pg_meta", r"postgres\.metadata_schema"),
        ("postgres", "connection_url_env", "postgresql://u:p@h/db", r"postgres\.connection_url_env"),
        ("postgres", "bulk_copy", "yes", r"postgres\.bulk_copy"),
        ("filesystem", "root", "relative/dir", r"filesystem\.root"),
        ("filesystem", "checksum", "md5", r"filesystem\.checksum"),
    ],
)
def test_invalid_values_rejected(section: str, key: str, value: object, match: str) -> None:
    d = example()
    d[section][key] = value
    with pytest.raises(config.ConfigError, match=match):
        parse(d)


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("clock_start", "2026-01-01T00:00:00", "timezone-aware"),
        ("clock_start", "not a date", "ISO-8601"),
        ("clock_end", "2025-12-31T00:00:00Z", "after clock_start"),
    ],
)
def test_invalid_clock_rejected(key: str, value: str, match: str) -> None:
    d = example()
    d["generator"]["simulation"][key] = value
    with pytest.raises(config.ConfigError, match=match):
        parse(d)


def test_missing_count_and_section_rejected() -> None:
    d = example()
    del d["generator"]["counts"]["orders"]
    with pytest.raises(config.ConfigError, match=r"generator\.counts\.orders: required"):
        parse(d)
    d = example()
    del d["postgres"]
    with pytest.raises(config.ConfigError, match=r"config\.postgres: required"):
        parse(d)


def test_unknown_top_level_section_rejected() -> None:
    d = example()
    d["postgress"] = {}
    with pytest.raises(config.ConfigError, match="unknown key.*postgress"):
        parse(d)


def test_bad_yaml_and_missing_file() -> None:
    with pytest.raises(config.ConfigError, match="invalid YAML"):
        config.parse(b"generator: [unclosed")
    with pytest.raises(config.ConfigError, match="must be a mapping"):
        config.parse(b"- a list")
    with pytest.raises(config.ConfigError, match="cannot read config"):
        config.load("/nonexistent/generator.yaml")


def test_database_url() -> None:
    cfg = config.load(EXAMPLE)
    assert config.database_url(cfg, {"ECOMMERCE_DATABASE_URL": URL}) == URL
    with pytest.raises(config.ConfigError, match="not set"):
        config.database_url(cfg, {"ECOMMERCE_DATABASE_URL": "  "})
    with pytest.raises(config.ConfigError, match="postgresql:// URL") as exc:
        config.database_url(cfg, {"ECOMMERCE_DATABASE_URL": "mysql://u:secret@h/db"})
    assert "secret" not in str(exc.value)
    # unencoded '@' or '/' in the password would make libpq echo part of it as a host name
    for bad in ("postgresql://u:top@SECRETPW@127.0.0.1/db", "postgresql://u:a/SECRETPW@127.0.0.1/db"):
        with pytest.raises(config.ConfigError, match="percent-encode") as exc:
            config.database_url(cfg, {"ECOMMERCE_DATABASE_URL": bad})
        assert "SECRETPW" not in str(exc.value)
    ok = "postgresql://u:top%40pw%2Fx@127.0.0.1:5433/db?sslmode=disable"
    assert config.database_url(cfg, {"ECOMMERCE_DATABASE_URL": ok}) == ok
    assert config.database_url(cfg, {"ECOMMERCE_DATABASE_URL": "postgresql:///db"}) == "postgresql:///db"


def test_cli_check_config(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("ECOMMERCE_DATABASE_URL", URL)
    assert main(["check-config", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "config OK" in out and "secret" not in out

    monkeypatch.delenv("ECOMMERCE_DATABASE_URL")
    assert main(["check-config", str(EXAMPLE)]) == 2
    assert "ECOMMERCE_DATABASE_URL is not set" in capsys.readouterr().err

    assert main(["check-config", "/nonexistent.yaml"]) == 2
