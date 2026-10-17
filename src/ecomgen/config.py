"""Load and validate the generator YAML config. Secrets never live here; the DB URL comes from an env var."""

import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

PROFILES = ("smoke", "mvp", "practice", "advanced", "stress")
RUN_MODES = ("historical", "incremental", "continuous")
COUNT_KEYS = ("customers", "products", "sellers", "warehouses", "orders", "clickstream_events")
# Parsed by later phases; accepted unvalidated for now so the example config loads.
DEFERRED_SECTIONS = ("scenario", "ai_readiness", "safety", "logging")
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class GeneratorConfig:
    version: int
    seed: int
    run_mode: str
    profile: str
    workers: int
    chunk_rows: int
    max_memory_mb: int
    clock_start: datetime
    clock_end: datetime
    time_multiplier: float
    counts: dict[str, int]


@dataclass(frozen=True)
class PostgresConfig:
    connection_url_env: str
    application_schema: str
    integration_schema: str
    metadata_schema: str
    bulk_copy: bool
    max_connection_pool: int


@dataclass(frozen=True)
class FilesystemConfig:
    root: PurePosixPath
    publish_via_staging: bool
    csv: bool
    jsonl: bool
    parquet: bool
    compression: str
    partition_by: str
    max_file_size_mb: int
    checksum: str


@dataclass(frozen=True)
class Config:
    generator: GeneratorConfig
    postgres: PostgresConfig
    filesystem: FilesystemConfig
    sha256: str  # of the raw file bytes; recorded per run as config_sha256


class _Section:
    """Reads keys from one mapping, reporting errors with their dotted path, and rejects leftovers."""

    def __init__(self, data: Any, path: str) -> None:
        if not isinstance(data, Mapping):
            raise ConfigError(f"{path}: must be a mapping")
        self._data = dict(data)
        self._path = path

    def _take(self, key: str) -> Any:
        if key not in self._data:
            raise ConfigError(f"{self._path}.{key}: required")
        return self._data.pop(key)

    def _err(self, key: str, msg: str) -> ConfigError:
        return ConfigError(f"{self._path}.{key}: {msg}")

    def section(self, key: str) -> "_Section":
        return _Section(self._take(key), f"{self._path}.{key}")

    def int(self, key: str, lo: int = 1, hi: int | None = None) -> int:
        v = self._take(key)
        if not isinstance(v, int) or isinstance(v, bool) or v < lo or (hi is not None and v > hi):
            raise self._err(key, f"must be an integer in [{lo}, {hi if hi is not None else '∞'}], got {v!r}")
        return v

    def float(self, key: str) -> float:
        v = self._take(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
            raise self._err(key, f"must be a positive number, got {v!r}")
        return float(v)

    def bool(self, key: str) -> bool:
        v = self._take(key)
        if not isinstance(v, bool):
            raise self._err(key, f"must be true or false, got {v!r}")
        return v

    def choice(self, key: str, options: tuple[str, ...]) -> str:
        v = self._take(key)
        if v not in options:
            raise self._err(key, f"must be one of {', '.join(options)}; got {v!r}")
        return str(v)

    def identifier(self, key: str) -> str:
        v = self._take(key)
        if not isinstance(v, str) or not _IDENTIFIER.fullmatch(v):
            raise self._err(key, f"must be a lowercase SQL identifier, got {v!r}")
        return v

    def utc(self, key: str) -> datetime:
        v = self._take(key)
        if isinstance(v, str):
            try:
                v = datetime.fromisoformat(v)
            except ValueError:
                raise self._err(key, f"not an ISO-8601 timestamp: {v!r}") from None
        if not isinstance(v, datetime) or v.tzinfo is None:
            raise self._err(key, f"must be a timezone-aware timestamp (e.g. 2026-01-01T00:00:00Z), got {v!r}")
        return v.astimezone(UTC)

    def done(self) -> None:
        if self._data:
            raise ConfigError(f"{self._path}: unknown key(s): {', '.join(sorted(map(str, self._data)))}")


def parse(raw: bytes) -> Config:
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML: {e}") from None
    top = _Section(data, "config")
    for name in DEFERRED_SECTIONS:
        top._data.pop(name, None)

    g = top.section("generator")
    version = g.int("version", 1, 1)
    seed = g.int("seed", 0)
    run_mode = g.choice("run_mode", RUN_MODES)
    profile = g.choice("profile", PROFILES)
    workers = g.int("workers", 1, 2)  # 16 GB homelab budget; raise only after profiling (Phase 7)
    chunk_rows = g.int("chunk_rows", 1, 1_000_000)
    max_memory_mb = g.int("max_memory_mb", 64)
    sim = g.section("simulation")
    clock_start, clock_end = sim.utc("clock_start"), sim.utc("clock_end")
    if clock_end <= clock_start:
        raise ConfigError("generator.simulation.clock_end: must be after clock_start")
    time_multiplier = sim.float("time_multiplier")
    sim.done()
    c = g.section("counts")
    counts = {k: c.int(k) for k in COUNT_KEYS}
    c.done()
    g.done()
    generator = GeneratorConfig(
        version, seed, run_mode, profile, workers, chunk_rows, max_memory_mb,
        clock_start, clock_end, time_multiplier, counts,
    )

    p = top.section("postgres")
    url_env = p._take("connection_url_env")
    if not isinstance(url_env, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", url_env):
        raise ConfigError(f"postgres.connection_url_env: must name an environment variable, got {url_env!r}")
    app, integ, meta = (p.identifier(k) for k in ("application_schema", "integration_schema", "metadata_schema"))
    if len({app, integ, meta}) != 3:
        raise ConfigError("postgres: application, integration and metadata schemas must be distinct")
    postgres = PostgresConfig(url_env, app, integ, meta, p.bool("bulk_copy"), p.int("max_connection_pool", 1, 32))
    p.done()

    f = top.section("filesystem")
    root = f._take("root")
    if not isinstance(root, str) or not PurePosixPath(root).is_absolute():
        raise ConfigError(f"filesystem.root: must be an absolute path, got {root!r}")
    filesystem = FilesystemConfig(
        PurePosixPath(root),
        f.bool("publish_via_staging"), f.bool("csv"), f.bool("jsonl"), f.bool("parquet"),
        f.choice("compression", ("snappy", "zstd", "gzip", "none")),
        f.choice("partition_by", ("event_date",)),
        f.int("max_file_size_mb", 1, 4096),
        f.choice("checksum", ("sha256",)),
    )
    f.done()
    top.done()
    return Config(generator, postgres, filesystem, hashlib.sha256(raw).hexdigest())


def load(path: str | os.PathLike[str]) -> Config:
    try:
        raw = Path(path).read_bytes()
    except OSError as e:
        raise ConfigError(f"cannot read config {path}: {e.strerror}") from None
    return parse(raw)


def database_url(cfg: Config, environ: Mapping[str, str] = os.environ) -> str:
    """Resolve the DB URL from the configured env var. Never log the returned value; it holds the password."""
    name = cfg.postgres.connection_url_env
    url = environ.get(name, "").strip()
    if not url:
        raise ConfigError(f"environment variable {name} is not set (see .env.example)")
    if not url.startswith(("postgresql://", "postgres://")):
        raise ConfigError(f"{name} must be a postgresql:// URL")
    return url
