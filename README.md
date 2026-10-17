# E-Commerce Data Engineering Homelab — Planning Pack

**Status:** Planning / specification only — no generator application is included yet.  
**Prepared:** 2026-10-17  
**Hardware baseline:** Ubuntu Server; AMD Ryzen 5 5600; 16 GB RAM; 2 TB storage (SSD/HDD not yet confirmed).  
**Goal:** Build a reproducible, realistic e-commerce **source-system simulator**, independently build reliable data engineering pipelines, and **later** use their curated outputs for AI engineering (RAG, recommendations, forecasting and model evaluation).

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
       +--> /srv/ecommerce-data (CSV / JSONL / Parquet / manifests)
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

