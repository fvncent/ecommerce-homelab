# Session Handoff

Update before compaction or ending a significant session. Overwrite; git history keeps the old ones.

**Updated:** 2026-10-17
**Branch / commit:** `feat/migrations-init-db` (see `git log`)

## Current phase and task
Phase 1 (CLI + schema MVP) — task 3 done: SQL migrations + `ecomgen init-db`.

## Completed
- PRs #1–#3 merged (workflow rules, package skeleton, config validation).
- `compose.yaml`: PostgreSQL 17.11 on `127.0.0.1:5433` (5432 = Homebrew PG, 55432 = another project), password from `.env`.
- `src/ecomgen/migrations/0001_core_schema.sql`: `commerce` sellers/categories/customers/products/orders/order_items/payment_attempts with FKs, money CHECKs (line_total and order total formulas, discount bounds), lifecycle enums, prefixed-ID checks, synthetic-email check, UTC timestamptz; empty `integration` schema; `generator_meta` generation_runs + generation_chunks ledger.
- `src/ecomgen/db.py`: migration runner — advisory lock, all pending in one transaction, checksum drift + unknown-version refusal, schema names from config.
- `ecomgen init-db CONFIG`.

## Key decisions
- Deterministic prefixed text IDs (`cus_123`) chosen by the generator, no sequences → chunk re-runs conflict instead of duplicating.
- Business tables carry no `run_id`; per-run tracking lives in `generator_meta` (may revisit in the generate task via migration 0002).
- Per-row money rules enforced by CHECKs; cross-row reconciliation (order header = sum of items, payments vs total) belongs to `validate`.
- Deferred to later phases: warehouses, inventory, shipments, returns, refunds, cart events, outbox; separate writer/reader DB roles; `init-db` does not CREATE DATABASE (compose does).

## Files modified
`compose.yaml`, `.env.example`, `pyproject.toml`, `uv.lock`, `src/ecomgen/{db,cli}.py`, `src/ecomgen/migrations/0001_core_schema.sql`, `tests/integration/test_init_db.py`, `README.md`, this file.

## Test results (macOS, Python 3.12.14, Docker postgres:17.11)
- `uv run pytest` with DB: 50 passed. Without `ECOMGEN_TEST_DATABASE_URL`: 26 passed, 24 skipped.
- `uv run mypy`: clean (8 files). Wheel contains the `.sql` migration.
- Manual: `init-db` applied 0001, second run "up to date"; `\dt commerce.*` shows 7 tables.

## Known failures / limitations
- Repo is in iCloud-synced `~/Documents`: iCloud re-hides `.venv` `.pth` files → `No module named 'ecomgen'`. Workaround: `UV_PROJECT_ENVIRONMENT` outside iCloud. iCloud also syncs `.env` (DB password) and `.venv`.
- No independent review of the schema yet; no CI; not run on Ubuntu.

## Next step
Phase 1 task 4: deterministic entity generation (seeded RNG, ID strategy, FK-safe ordering) + bounded-chunk COPY with the chunk marker in the same transaction; `generate --profile smoke` for 1,000 orders.
