# 01 — Project Charter and Requirements

## Vision

Create a simulated e-commerce business that continuously generates **realistic, consistent, replayable** relational records and external files. Use it to develop a separate data engineering practice platform with measurable reliability, recovery, correctness and scalability.

## Current environment and constraints

- Single Ubuntu Server homelab; Ryzen 5 5600; 16 GB RAM; 2 TB storage.
- Storage type, motherboard model, physical DIMM inventory, exact Ubuntu version and existing services **still to be confirmed**.
- Deploy via Docker Compose when practical; support an existing PostgreSQL instance.
- Local-only operation must work without cloud accounts or Kafka.
- Only synthetic customers and transactions; no real personal or payment information.
- Conservative parallelism, bounded memory, file rotation, storage quotas and restartability are mandatory.

## Project components

1. **Data generation core:** Python library with deterministic seed and configurable clock, domain state machines and relationships.
2. **Source adapters:** PostgreSQL transactional writes and local file drops (CSV, JSONL, Parquet).
3. **Management interfaces:** CLI required first; Streamlit dashboard later using the same generation APIs.
4. **Scenario engine:** seasonality, customer segments, skew, failures, delayed feeds and configurable traffic spikes.
5. **Verification:** data contracts, manifests, generation checkpoints, reconciliation, benchmark logs.
6. **Student-owned pipelines (out of generator scope):** Airflow, Spark, Kafka/Debezium, dbt, data lake, warehouse, dashboards.

## MVP: definition of done

**One command** creates or connects to the isolated `ecommerce_oltp` PostgreSQL database, initializes versioned migrations, generates at least 10,000 coherent orders and related entities, and writes at least one external payment batch file (CSV) plus clickstream events (JSONL) under `/srv/ecommerce-data` (path configurable).

The CLI must: (a) accept a configuration and seed, (b) validate keys and chronology, (c) produce a manifest with row counts, locations and checksums, (d) support clean restart/retry of a deliberately interrupted run, (e) keep secrets out of logs and code, and (f) run on a 16 GB RAM server without requiring Kafka, Spark, MinIO or Streamlit.

## Non-goals for the MVP

- No automatically built Bronze/Silver/Gold transformations.
- No need for Kubernetes, distributed clusters, public endpoints or microservices.
- No guarantee of billion-row throughput on current hardware.
- No automatic cross-store atomic transaction between PostgreSQL and filesystem.
- No complex frontend before core generation and recovery are validated.

## Workloads and growth profiles

| Profile | Orders | Customers | Approx. events | Goal |
|---|---:|---:|---:|---|
| Smoke | 1,000 | 500 | 10,000 | Validate environment |
| MVP | 10,000 | 5,000 | 100,000 | Prove correctness |
| Practice | 100,000 | 50,000 | 1,000,000 | Daily batch/CDC exercises |
| Advanced | 1,000,000 | 200,000 | 10,000,000 | Throughput, skew and partitions |
| Stress | 10,000,000 | 1,000,000 | 100,000,000 | Optional, benchmark first |

These are **profiles, not performance promises**. Record actual time, peak RSS, WAL growth, file footprint and CPU load for each completed run.

## Success measures

- 100% canonical FK integrity and valid domain state transitions after successful generation.
- 100% manifest-to-output count agreement for committed partitions.
- Reproducible logical records at same seed + config + generator version; byte-identical files only when serialization is controlled.
- Retries do not create duplicate logical orders, batches or published manifests.
- Corrupted raw feeds are traceable to a private anomaly ledger unavailable to the practice pipeline.
- Historical backfills and simulated live updates share the same business rules.

## Roles and boundaries

- **Claude:** write the source simulator in deliverable phases, including tests and documentation.
- **You:** operate the homelab, own the downstream pipeline, choose scenarios and verify outcomes.
- **Generator's responsibility:** produce source records, delivery schedules and fault scenarios.
- **Pipeline's responsibility:** ingest, validate, deduplicate, transform, load, observe and recover.

## Long-term AI engineering objective (deferred)

After the learner-owned data engineering platform is reliable, reuse curated time-aware datasets and synthetic text to practice recommendation systems, RAG, semantic search, forecasting, fraud detection, support NLP and tool-using agents. **The generator does not train or serve models.** See [10_AI_ENGINEERING_READINESS.md](10_AI_ENGINEERING_READINESS.md) and [11_AI_EVALUATION_AND_EXPERIMENTS.md](11_AI_EVALUATION_AND_EXPERIMENTS.md).

Design decisions to make **now**: stable cross-source keys; session and event times; `source_system`, `schema_version` and `run_id`; versionable product/catalog data; strict separation of raw data and private truth labels. Rich AI-specific corpora and evaluation fixtures remain *opt-in after MVP*.
