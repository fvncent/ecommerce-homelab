# 04 — Phased Implementation Roadmap

No dates are promised; advance only after a phase's exit criteria pass. Favor vertical end-to-end slices over building many incomplete modules.

| Phase | Deliverables | Exit / gate |
|---|---|---|
| 0: Inventory | Confirm RAM, motherboard, disk, ports, services; repo skeleton; `.env.example`; architecture decision log | System inventory and safe directory plan documented |
| 1: CLI + schema MVP | Python package/CLI, config validation, migrations, customers/products/orders/items/payments, `COPY` bulk loading, run manifest | Generate 10k orders; FK/amount checks pass; restart works |
| 2: Independent file feeds | CSV supplier/settlement files, JSONL clickstream, Parquet optional snapshot; partitioning and manifests | Both PostgreSQL and local files agree on business IDs |
| 3: Simulation clock | Incremental orders and state changes, event/outbox, seller/warehouse inventory, replay by date | Same seed/config produces equal logical data; catch-up works |
| 4: Realism + anomalies | Seasonality, skew, failure cases, private anomaly ledger, file landing delays | Known injected anomalies measurable; canonical records remain valid |
| 5: Streamlit + operations | Job UI, logs/metrics, queue status, configuration UI, cancellation | UI and CLI invoke same engine; no unbounded background workers |
| 6: CDC readiness | Logical replication checklist, PK/repl identity, outbox changes, safe Debezium setup notes | UPDATE/DELETE visible to future CDC; WAL limits documented |
| 7: Performance | Generator chunking, bench profiles, 1M+ orders only after profiling | Memory and disk tracked; corruption/restarts tested under load |
| 8: Learner pipelines | **Separate project owned by you:** Airflow, file ingest, CDC, bronze/silver/gold, dbt tests | Independent recovery, late events, reconciliation and monitoring |
| 9: AI-ready source extension | **Generator, optional modules:** versioned text/knowledge documents, impressions, support text, private evaluation truth | Source/eval contracts pass; generator still runs without AI services |
| 10: Learner AI engineering | **Separate project owned by you:** semantic search, RAG, recommendations, forecasting, fraud, AI evaluation and agents | Time-safe data splits, grounded results, baseline metrics and reproducibility |

## Suggested implementation tickets by phase

### Phase 1 detailed tasks
- Initialize repository, Python package, locked dependencies and type checking/test harness.
- Add config YAML, environment validation and seeded clock.
- Create SQL migrations and isolated PostgreSQL schemas, with indices/constraints.
- Implement deterministic entity identity and foreign-key-safe generation ordering.
- Implement bounded chunk production and COPY with transactional chunk marker.
- Implement `init-db`, `generate --profile smoke`, `validate --run-id`, `status` and `resume` commands.
- Add tests for monetary math, state transitions, FK constraints, duplicate re-run and corruption rejection.

### Phase 2 detailed tasks
- Define source feed contracts per partner and stable IDs shared with PostgreSQL.
- Write files to staging before atomic publish, record checksums and row counts.
- Build `replay-files --from --to` command with deterministic filenames.
- Add filenames/partition cleanup policy, never delete existing runs without explicit command.

### Phase 3 detailed tasks
- Simulation speed setting: wall clock 1x vs 24x / custom event-clock stepping.
- State-machine-driven shipping/payment/return events and transaction outbox.
- Backfill timeline generation; replay and rebuild from same config/seed.

### Phase 4 detailed tasks
- Fault injection parameters separate from business generation parameters.
- Ledger with exact file/line/event affected; hold back from ingest pipeline.
- Failure cases and retries in continuous integration tests.

### Phase 5+ tasks
- Dashboard start/stop/status UI and job history.
- Optional Kafka producer separate from logical event generation.
- PostgreSQL replication slot safety runbook and benchmark report templates.

## Deliberate constraints

1. Do **not** ship dashboard instead of working generation core.
2. Do **not** silently skip tables or tests to hit a target row count.
3. Do **not** implement your downstream ETL; include only sample queries/verification tooling necessary to test the generator.
4. Do **not** store passwords in YAML or Git. Use environment variables.
5. Do **not** turn on logical decoding with unmonitored retained WAL.

## Artifact handoff between Claude sessions

At each completed phase, ask Claude to provide:
- exact repository tree and complete changed file contents;
- install, migrate, run and test commands;
- test output or explicit statement that code wasn't executed;
- deployment/rollback notes;
- unresolved issues, next phase tasks and any assumptions;
- updated README and configuration examples.

## AI-ready data milestone: deferral rules

Document ID/time/version rules and source-system ownership during schema design **now**. After source/pipeline reliability milestones, implement opt-in source text, knowledge documents, recommendation impressions and private labels in the generator. Build the actual AI/ML/RAG applications in a **third, separately managed project**. Training labels, RAG expected answers and synthetic fraud truth must never be exposed as regular inference inputs. See AI documents 10 and 11.
