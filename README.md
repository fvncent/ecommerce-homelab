# E-Commerce Data Engineering Homelab — Planning Pack

**Status:** Planning / specification only — no generator application is included yet.  
**Prepared:** 2026-10-17  
**Hardware baseline:** Ubuntu Server; AMD Ryzen 5 5600; 16 GB RAM; 1 TB NVMe for project data (`/srv/homelab`); host PostgreSQL 18.  
**Goal:** Build a reproducible, realistic e-commerce **source-system simulator**, independently build reliable data engineering pipelines, and **later** use their curated outputs for AI engineering (RAG, recommendations, forecasting and model evaluation).

## Development

Requires [uv](https://docs.astral.sh/uv/) (installs Python 3.12 if missing) and Docker.

```bash
uv sync                                  # create .venv from uv.lock
cp .env.example .env                     # set POSTGRES_PASSWORD and both URLs (port 5433)
set -a; . ./.env; set +a                 # .env is not auto-loaded
docker compose up -d --wait              # local PostgreSQL 17 on 127.0.0.1:5433
uv run ecomgen check-config config/generator.example.yaml
uv run ecomgen init-db config/generator.example.yaml   # idempotent; applies pending migrations
uv run ecomgen generate config/generator.example.yaml --profile smoke  # 1,000 orders; omit --profile for config counts
uv run ecomgen validate config/generator.example.yaml  # invariants + ledger; --run-id RUN (default: latest)
uv run ecomgen status config/generator.example.yaml    # run state + chunk progress (read-only)
uv run ecomgen resume config/generator.example.yaml    # finish a crashed/FAILED run with the identical config
uv run pytest                            # integration tests skip unless ECOMGEN_TEST_DATABASE_URL is set
uv run mypy
```

Measured runs (time, peak RSS, WAL, DB size, restart) are in [docs/BENCHMARKS.md](docs/BENCHMARKS.md). Ubuntu homelab runbook: [docs/HOMELAB_DEPLOYMENT.md](docs/HOMELAB_DEPLOYMENT.md).

Migrations live in `src/ecomgen/migrations/NNNN_*.sql` and are applied in one transaction. Never edit an applied migration; add a new one (init-db refuses checksum drift).

Entity generation (`src/ecomgen/generate.py`) is pure and deterministic: entity N is built from its own RNG seeded by `sha256(seed:entity:N)`, so any chunk regenerates alone and output doesn't depend on `chunk_rows`. IDs are `prefix_N`; item/payment IDs use a fixed stride per order (`itm_{(order-1)*5+line}`, `pay_{(order-1)*3+attempt}`), so they are sparse. Order/payment states are the simulated lifecycle as of `clock_end`. Limitations: single currency (USD); uniform customer/product choice (skew is Phase 4).

`generate` (`src/ecomgen/load.py`) starts a run in `generator_meta.generation_runs` and writes each chunk with `COPY` (or `INSERT` when `bulk_copy: false`) plus its `generation_chunks` marker in one transaction: a marker means the chunk is durable, no marker means none of it was committed. Chunks already marked for a run are skipped, which is what `resume` will build on. Each marker also records rows per table (`table_rows`), and run totals come from the ledger. Because IDs are deterministic, `generate` refuses a database that already holds a run or any source rows; to finish a crashed (`RUNNING`) or `FAILED` run use `resume`; to start over, recreate the database (or its application + metadata schemas) and run `init-db`. `--profile smoke` uses built-in counts; without the flag the config's counts are used even if its `profile` is `smoke`. An error marks the run `FAILED`; a hard crash leaves it `RUNNING`. One run at a time (advisory lock). `workers` is not used yet (single process).

`resume` takes the same advisory lock as `generate`, so holding it proves no other process is writing and a `RUNNING` run is stale. Generation is a pure function of the `World` (seed, clock, counts), `generate.DATA_VERSION` and `chunk_rows`, all recorded per run in `resume_fields`. `resume` rebuilds the `World` from that record, so edits to seed/clock/counts, comments, non-row settings and the package version can't change a resumed run; it refuses (naming the field) only if `DATA_VERSION`, `chunk_rows` or the application schema differ. Bump `DATA_VERSION` whenever generated rows change; never edit an existing `GOLDEN` entry for a data change. It writes only the chunks missing from the ledger; the result is identical to an uninterrupted run (tested with a real `SIGKILL`). `status` is read-only: it reads `pg_locks` instead of taking the lock, and shows the backend PID holding the run lock, if any. A client that vanished without closing its connection (power loss) can leave its session holding the lock until TCP keepalive drops it; `resume` then names the PID to end with `pg_terminate_backend`. A run's last error is kept across resumes.

`validate` (`src/ecomgen/validate.py`) runs, in one read-only snapshot, the cross-row rules the schema can't enforce: order header = Σ items, contiguous line/attempt numbers, product currency = order currency, every payment attempt = order total, at most one live (non-FAILED) attempt and it is the last, order status consistent with the live payment attempt (a cancelled order may have no attempts), timestamps ordered (customer/product before order, seller before product, payment after order), acyclic categories; and per run: run COMPLETED, every planned chunk in the ledger, ledger row totals = table counts. Each check reports a count and up to 5 sample IDs. Exit codes: 0 all pass, 1 a check failed, 2 config error, 3 could not validate (no run / run not found / DB error). Scope: data checks cover the whole application schema (it holds one run's data); `--run-id` scopes only the ledger checks.

> **macOS + iCloud:** if the repo is under an iCloud-synced folder (e.g. `~/Documents`), iCloud hides `.venv/**/*.pth` files and the editable install breaks (`No module named 'ecomgen'`). Keep the venv outside iCloud, e.g. `export UV_PROJECT_ENVIRONMENT=~/.venvs/ecomgen`.

## What's in this pack

| File | Purpose |
|---|---|
| [docs/01_PROJECT_CHARTER.md](docs/01_PROJECT_CHARTER.md) | Scope, goals, MVP, constraints and definition of success |
| [docs/02_SYSTEM_ARCHITECTURE.md](docs/02_SYSTEM_ARCHITECTURE.md) | Homelab topology, storage, data flows, design decisions |
| [docs/03_DATA_MODEL_AND_CONTRACTS.md](docs/03_DATA_MODEL_AND_CONTRACTS.md) | Tables, keys, events, business integrity, output formats |
| [docs/04_IMPLEMENTATION_ROADMAP.md](docs/04_IMPLEMENTATION_ROADMAP.md) | Build phases with deliverables and exit criteria |
| [docs/05_TESTING_AND_ACCEPTANCE.md](docs/05_TESTING_AND_ACCEPTANCE.md) | Validation and failure/restart tests |
| [docs/06_DEPLOYMENT_OPERATIONS.md](docs/06_DEPLOYMENT_OPERATIONS.md) | Low-memory deployment, PostgreSQL, backup, security |
| [docs/07_SIMULATION_SCENARIOS.md](docs/07_SIMULATION_SCENARIOS.md) | Realistic business workloads and controlled failure cases |
| [docs/08_FUTURE_ENHANCEMENTS.md](docs/08_FUTURE_ENHANCEMENTS.md) | Feature and hardware roadmap |
| [docs/09_RAM_AND_STORAGE_CHECK.md](docs/09_RAM_AND_STORAGE_CHECK.md) | Ubuntu commands to determine RAM compatibility |
| [docs/10_AI_ENGINEERING_READINESS.md](docs/10_AI_ENGINEERING_READINESS.md) | Long-term AI-friendly data contracts, source types, timestamps and resource strategy |
| [docs/11_AI_EVALUATION_AND_EXPERIMENTS.md](docs/11_AI_EVALUATION_AND_EXPERIMENTS.md) | Future RAG, recommendation, forecasting, fraud and agent evaluation plan |
| [prompts/CLAUDE_MASTER_BUILD_PROMPT.md](prompts/CLAUDE_MASTER_BUILD_PROMPT.md) | Complete master prompt for phased implementation |
| [prompts/CLAUDE_PHASE_1_STARTER_PROMPT.md](prompts/CLAUDE_PHASE_1_STARTER_PROMPT.md) | Shorter action-oriented prompt for the first working build |
| [prompts/CLAUDE_AI_READINESS_ADDENDUM.md](prompts/CLAUDE_AI_READINESS_ADDENDUM.md) | Copy into Claude to preserve AI requirements without expanding the MVP |
| [config/generator.example.yaml](config/generator.example.yaml) | Illustrative proposed generator settings; not executable yet |
| [scripts/inspect_ubuntu_hardware.sh](scripts/inspect_ubuntu_hardware.sh) | Read-only RAM, motherboard and disk summary on Ubuntu |

## Suggested starting sequence

1. **Inspect hardware** using the included guide/script. Confirm motherboard, RAM module count and storage media type.
2. **Review the charter and architecture** to align source systems and learning boundaries.
3. **Copy the Claude master build prompt**, plus the charter and architecture as context, into Claude.
4. **Implement only the CLI MVP initially:** PostgreSQL schema, 10,000 orders with related records, CSV/JSONL files, validation, tests.
5. **Prove restart, consistency and bounded memory** before adding Streamlit, CDC or Spark.
6. **Build the downstream pipeline yourself** as a separate repository / Compose stack.
7. **After the data platform is reliable**, pursue AI projects using the data readiness and evaluation documents; do not add LLM/embedding workloads to the generator MVP.

## Target architecture at a glance

```text
Python CLI / optional Streamlit dashboard
       | shared deterministic simulation engine
       +--> PostgreSQL (normalized e-commerce OLTP + outbox)
       +--> /srv/homelab/ecommerce/data (CSV / JSONL / Parquet / manifests)
                         |
           YOUR INDEPENDENT PIPELINE
        batch files / PostgreSQL extraction / CDC
                --> bronze --> silver --> gold
                     |
       YOUR LATER AI ENGINEERING PROJECT
   RAG / search / recommendations / forecasting / eval
```

**Critical boundary:** The generator does NOT build your ingestion, orchestration, warehouse, medallion transformations or analytics pipeline.

## Useful reference documentation

- AMD Ryzen 5 5600 specifications: https://www.amd.com/en/support/downloads/drivers.html/processors/ryzen/ryzen-5000-series/amd-ryzen-5-5600.html
- Ubuntu `dmidecode`: https://manpages.ubuntu.com/manpages/bionic/man8/dmidecode.8.html
- Ubuntu `lshw`: https://manpages.ubuntu.com/manpages/jammy/man1/lshw.1.html
- PostgreSQL logical replication: https://www.postgresql.org/docs/current/logical-replication-config.html
- PostgreSQL replication configuration and WAL retention: https://www.postgresql.org/docs/current/runtime-config-replication.html

