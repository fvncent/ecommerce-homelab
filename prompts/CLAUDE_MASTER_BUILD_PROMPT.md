# Master Claude Prompt — Production-Like E-Commerce Source-System Simulator

You are a senior/principal data engineer, database architect, reliability engineer and Python developer. Implement an **executable, maintainable, well-tested synthetic e-commerce SOURCE-SYSTEM SIMULATOR** for my single-machine Ubuntu Server homelab. Your job is to create a realistic source to feed the data pipelines I will build separately, NOT to build the entire downstream analytics stack for me.

## Environment and design constraints

- Hardware: AMD Ryzen 5 5600 (6 cores/12 threads), 16 GB RAM, 2 TB storage. The drive type and motherboard model are not yet confirmed.
- OS: Ubuntu Server on a private LAN.
- Primary operational datastore: **PostgreSQL** (new isolated `ecommerce_oltp` database or externally supplied existing PostgreSQL instance).
- File source delivery: configurable local directory, default `/srv/ecommerce-data`.
- File types: CSV for supplier/payment/warehouse source feeds; JSONL for clickstream, system events and optional replay files; Parquet with Snappy for optional snapshots and high-volume exports; JSON for metadata/manifests.
- Interfaces: **Python CLI FIRST**, then optional **Streamlit** UI using exactly the same generation engine.
- Container packaging: Compose file; support connecting to existing PostgreSQL rather than mandating a second database.
- MVP must run fully offline without Kafka, Debezium, Spark, MinIO, Airflow or cloud accounts.
- Protect the 16 GB host: bounded memory, few workers, small connection pool, chunked output and meaningful disk-space checks. Provide conservative defaults.
- All generated persons, addresses and transactions are synthetic. Never use real customer data, real credit card numbers or stored secrets.
- Do not expose PostgreSQL or the dashboard to the public internet by default.

## Domain design

Implement a coherent e-commerce marketplace with these entities and business links, phased appropriately:

- Customers, sellers, hierarchical product categories, products, warehouses.
- Orders with multiple order items, prices, taxes, discounts and exact monetary reconciliation.
- Payment attempts, state changes, settlements and refunds.
- Warehouse-level inventory and inventory movements.
- Order fulfillment: split shipments, statuses and delivery progress.
- Returns and refunds, delayed compared with original order.
- Carts, sessions, product views, clickstream, reviews, promotions and support tickets (these may follow MVP).
- External source systems: suppliers, payment processor, couriers and website analytics; files may arrive asynchronously.

Require stable shared IDs across database and files, enforced PostgreSQL primary/foreign keys, legal state transitions, UTC event-time semantics and clearly defined financial calculations. Monetary arithmetic must use Decimal / PostgreSQL NUMERIC or documented integer minor units. Do not assume 1:1 orders:payments or orders:shipments.

## Business realism

Implement reasonable configurable distributions rather than independent random records: day-of-week/seasonal peaks, customer retention and repeat purchases, product popularity skew, geographic behavior, order and item quantity patterns, payments failures and retries, inventory reservations/replenishment, shipment lead times, returns and refunds. Later add Black Friday, a payment provider outage, late supplier batch deliveries, schema revisions and hot-key stress.

Include a **simulation clock** that can run historical backfills, daily increments or continuously at a speed multiplier, while preserving business/event/arrival timestamp distinctions.

## Source data and CDC readiness

- PostgreSQL represents operational truth. Model normalized tables in schema `commerce` and a transactionally-written `integration.outbox_events` table.
- Provide versioned migrations and sensible indexes, constraints, stable keys and transactional insertion/update/delete behavior.
- For initial generation use scalable PostgreSQL `COPY` or equivalent bulk operation as appropriate; for subsequent updates use real transactional state mutations so future CDC sees actual changes.
- Use `generator_meta` for run state, checkpoints, progress and config hashes.
- Document later Debezium/logical replication setup but **do not require CDC tools during MVP**.
- Supply real, mutually related external source feeds. Do not simply dump every database table to CSV as the only file source.
- Raw file deliveries can be late, duplicated, partially missing or versioned; canonical PostgreSQL business records must remain valid except in explicitly separate failure drills.
- Treat a filesystem operation and a PostgreSQL transaction as **non-atomic**: design deterministic IDs, unique chunk keys, staging/publish, checkpoints and reconciliation rather than claiming exactly-once distributed commits.

## Faults, observability and manifests

Build a configurable anomaly injection system for raw sources, including missing mandatory values, malformed CSV/JSONL, duplicate event delivery, late/out-of-order arrivals, changed columns/types, corrected partner feeds, intentionally orphaned raw IDs and hot-key distributions.

Keep a clean canonical source separate from corrupted raw feeds. Each anomaly has a **private machine-readable ground-truth record** listing run_id, source file/event key, anomaly type, expected detection and parameters. The ground-truth directory must not be accessible to learner pipeline containers by default.

Every completed run/partition must publish a manifest containing row counts, schema version, checksum, byte counts, event-time range, generator version, seed and configuration hash. Incomplete files should be staged, not visible as completed inputs.

Log rows/sec, bytes, memory peak where feasible, duration, failure counts, run status, file paths and disk/WAL growth, with no passwords or sensitive machine identity in logs.

## Deployment and configuration

Provide complete:

- Python package and importable generator engine.
- CLI commands such as `init-db`, `generate`, `simulate`, `validate`, `status`, `resume`, `replay-files` and a *dangerous* `reset` only with explicit confirmation and dedicated DB checks.
- YAML sample profiles: smoke, MVP, medium and stress (stress defaults disabled).
- PostgreSQL connection through `ECOMMERCE_DATABASE_URL` or similar environment variable, `.env.example`, never hardcoded passwords.
- Dockerfiles and Compose supporting isolated PostgreSQL **or external DB mode**, volumes, healthchecks and constrained memory usage where practical.
- File output directory rooted in `/srv/ecommerce-data` unless configured otherwise.
- SQL migrations, tests, README, schema dictionary, ERD (Mermaid), scenario catalog, example data and example commands.
- A plain Python CLI fallback so Streamlit / Docker isn't a hard dependency for generation.

## Future AI engineering compatibility (design for now; build later)

I plan to use these same synthetic sources for later AI engineering projects, including product recommendations, semantic product search, RAG support chatbots, demand forecasting, synthetic fraud detection, support-ticket NLP and safe order-support AI agents.

- Preserve stable session/product/customer/order/event identifiers; source provenance; timestamps and document/policy versions needed for point-in-time features and retrieval citations.
- Reserve extensibility for product titles/descriptions/attributes, search queries and **impression/exposure logs** (not only clicks), product reviews, support conversations, and original synthetic knowledge-base Markdown/JSON documents.
- Define a restricted private truth/evaluation area for relevance judgments, answerable/unanswerable RAG queries, delayed fraud labels and deterministic scenario metadata. Never leak these labels into regular inference/training features.
- Plan chronological train/validation/test splits, cold-start and concept-drift scenarios, and versioned source/answer documents.
- **Do not implement AI models, embedding pipelines, vector databases, LLM serving, RAG apps or agent systems in the source generator MVP.** Add AI-focused data generators as optional source modules only after core correctness/recovery goals pass. Actual AI engineering is a separate learner-owned project.
- Follow `docs/10_AI_ENGINEERING_READINESS.md` and `docs/11_AI_EVALUATION_AND_EXPERIMENTS.md` when attached. Also see `prompts/CLAUDE_AI_READINESS_ADDENDUM.md` for a standalone request.

## Distinctly excluded

Do NOT create my downstream data engineering pipeline, ingestion DAGs, Kafka consumers, Spark ETL, dbt transformations, warehouse/BI models or medallion data layers. I will implement those myself. You may provide contracts/expected values and tiny read-only verification queries used to validate the generator.

## Implementation phases and gates

1. **Phase 0 — Architecture review**: ERD, schema, relationships, business logic/state machines, dependencies, security assumptions, repository tree and resource constraints.
2. **Phase 1 — Runnable minimal vertical slice**: CLI + migrations + PostgreSQL + seeded realistic customers/products/orders/order_items/payments; write supplier/payment CSV and clickstream JSONL; 10,000 orders, manifests, tests and documentation; restartable/idempotent chunks.
3. **Phase 2 — Incremental simulation**: ongoing INSERT/UPDATE/DELETE, fulfillment/returns/inventory, outbox, simulation clock, backfill/replay.
4. **Phase 3 — Data anomalies**: file landing irregularities, controlled corruption/duplicates, private scoring ledger and validation suite.
5. **Phase 4 — Streamlit UI**: start/stop jobs safely, select profile/scenario, see status, errors, output locations and operational metrics; must reuse the CLI core.
6. **Phase 5 — CDC integration guide and performance**: safe logical replication guidance, slot/WAL monitoring, optional Kafka producer, bounded-memory stress tests.
7. **Later source AI-readiness modules (optional)**: synthetic versioned knowledge documents, impression/query logs, review/ticket text, private evaluation truth and source contracts; no LLM runtime or model training.

**Build one phase at a time.** Do not respond with an architecture-only proposal when you are instructed to implement Phase 1. Deliver full runnable files and tests per phase, never pseudo-code or unimplemented TODOs disguised as complete features.

## Phase 1 acceptance tests

- On a fresh installation, initialization/migrations run successfully.
- One documented CLI command inserts 10,000 coherent orders, related items, payments and referenced customers/products into PostgreSQL.
- At least one payment settlement CSV batch and one JSONL clickstream file contain shared, valid business identifiers.
- Validation finds no foreign-key violations, invalid domain transitions, duplicate logical order IDs or unexplained monetary mismatches in canonical data.
- A manifest includes seeded run ID, counts, file paths and checksums.
- Re-running or resuming a completed chunk does not add duplicate records/files.
- An interrupted run can be resumed; include an automated test simulating interruption at a chunk boundary and explain cross-DB/filesystem reconciliation behavior.
- The smoke profile runs on a 16 GB server without loading the whole dataset into RAM.
- All source code, migrations, setup instructions and tests are actually provided.

## Output workflow

For EACH phase give:

1. Brief design and any assumptions.
2. Full repository tree for touched parts.
3. Every changed file with its path and complete contents, or a downloadable project artifact if supported.
4. Exact installation, configuration, database migration, run and test commands.
5. Expected output samples and how to inspect results.
6. Tests and explicit verification status; **never claim code was executed unless it was actually run**.
7. Remaining limitations, risks, and next phase gates.

If your response length is insufficient, finish a usable, tested vertical slice and clearly state which files remain. Never silently omit code or claim unfinished features are done. Avoid broad clarifying questions; make reasonable defaults and clearly label assumptions. **Start now with Phase 0 architecture and an executable Phase 1 skeleton only, not all features at once.**
