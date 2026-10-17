# Shorter Claude Starter Prompt — Build the First Working Slice

Act as a senior Python and PostgreSQL data engineer. I have an Ubuntu Server homelab (Ryzen 5 5600, 16 GB RAM, 2 TB storage). Implement an **actual working, memory-efficient, restartable e-commerce synthetic SOURCE DATA generator** — not ETL or a warehouse. I will build the downstream pipelines myself.

Required destinations:
1. PostgreSQL database `ecommerce_oltp` for normalized `customers`, `products`, `orders`, `order_items`, `payment_attempts` and appropriate reference tables. Provide migrations, foreign keys and financial integrity checks.
2. Local `/srv/ecommerce-data` for payment processor CSV files and website clickstream JSONL files. Files must reference real PostgreSQL business IDs. Keep incomplete file output in a staging folder and publish completion manifests with checksums.

Implement a Python package with a YAML configuration, seeded/deterministic data, CLI commands `init-db`, `generate`, `validate`, `status`, and `resume`, chunked generation with PostgreSQL COPY where appropriate, `.env.example`, automated tests, Docker Compose with persistent PostgreSQL and file mounts, and support for connecting to an existing PostgreSQL server.

Start with 10,000 realistic orders, not millions. Use Decimal / NUMERIC for money, timestamps in UTC, reproducible IDs, and valid order relationships. Enforce idempotent retries and a persistent run ledger. Use only synthetic personal details. Keep generator memory bounded, <=2 workers as a default, and avoid installing Spark, Kafka, Airflow or Streamlit at this first stage.

Deliver complete runnable project files, a Mermaid ERD, repository structure, database schema, README, sample outputs, tests, and exact Ubuntu/Docker commands. Explain what you actually tested and what is still hypothetical. Do not pretend cross-store writes to PostgreSQL and the filesystem are atomic; include reconciliation/retry strategy.

**Implement Phase 1 now.** Before writing advanced features, verify that a 1,000-order smoke test passes all relational and financial checks, then provide a command for the 10,000-order MVP.

## Long-term compatibility — keep Phase 1 small

I will **later** build AI engineering projects (RAG, product recommendations, semantic search, forecasting and fraud models) on top of my own data engineering pipelines. Preserve stable `customer_id`, `product_id`, `order_id`, `session_id`, `event_id` and UTC timestamps with schema/source versioning now, but **do not build model training, embeddings, vector databases, RAG, LLMs or agents in Phase 1**. Keep later text corpora, impression logs, AI labels and evaluation fixtures on the roadmap. An AI-readiness addendum and dedicated planning documents will be provided separately.
