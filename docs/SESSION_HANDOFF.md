# Session Handoff

Update before compaction or ending a significant session. Overwrite; git history keeps the old ones. Task list: `TASKS.md`.

**Updated:** 2026-10-17
**Branch / commit:** `feat/migrations-init-db` — `55bbb9a` + uncommitted review fixes (not pushed)

## Current phase and task
Phase 1 (CLI + schema MVP) — task 3: SQL migrations + `ecomgen init-db`, reviewed and fixed, awaiting approval for PR #4.

## Completed
- PRs #1–#3 merged (workflow rules, package skeleton, config validation).
- `compose.yaml`: PostgreSQL 17.11 on `127.0.0.1:5433`, password from `.env`.
- `0001_core_schema.sql`: `commerce` MVP tables with FKs, money CHECKs, lifecycle enums, canonical prefixed IDs, synthetic-email check; `integration` schema; `generator_meta` run/chunk ledger.
- `db.py` migration runner + `ecomgen init-db CONFIG`.
- Review: `/ponytail-review` + independent reviewer subagent (fresh context). All findings fixed:

| # | Sev | Finding | Fix |
|---|---|---|---|
| 1 | HIGH | Unencoded `@` in DB password → libpq echoes password tail as host in error | `database_url` rejects raw `@`/`/` in userinfo; tests |
| 2 | HIGH | Renaming `application_schema` → init-db said "up to date", 0 tables | Checksum the *rendered* SQL; refuse with clear message; test |
| 3 | MED | EUR order with USD payment accepted | `UNIQUE(order_id,currency)` + composite FK from payments; product↔order currency → `validate` |
| 4 | MED | Only secret-leak test was skipped without DB; skips easy to miss | Moved to unit tests; pytest `-rs` always lists skips |
| 5 | MED | Schema names substituted unquoted (`order` → SyntaxError); `pg_*` accepted | `sql.Identifier` quoting; config rejects `pg_*`; test with schema `order` |
| 6 | LOW | `ord_1` and `ord_01` both valid PKs → replay could duplicate | ID regex `^xxx_[1-9][0-9]*$` |
| 7 | LOW | Two concurrent `RUNNING` runs possible | Partial unique index: one RUNNING run |
| 8 | LOW | Bad migration filename → raw ValueError | MigrationError |

## Key decisions
- Deterministic canonical text IDs chosen by the generator; chunk re-runs conflict instead of duplicating.
- Migration 0001 edited in place (never merged/deployed); local dev DB was empty and was reset.
- Cross-row money rules (header = Σ items, payments vs total) belong to `validate`.
- A crashed run stays `RUNNING` and blocks new runs until `resume`/explicit fail (by design).

## Files modified (this task)
`compose.yaml`, `.env.example`, `pyproject.toml`, `uv.lock`, `src/ecomgen/{db,cli,config}.py`, `src/ecomgen/migrations/0001_core_schema.sql`, `tests/integration/test_init_db.py`, `tests/test_cli.py`, `tests/test_config.py`, `README.md`, `TASKS.md`, this file.

## Test results (macOS, Python 3.12.14, Docker postgres:17.11)
- Full suite with DB: **54 passed, 0 failed, 0 skipped** (28 unit + 26 integration, each run separately to confirm).
- Without `ECOMGEN_TEST_DATABASE_URL`: 28 passed, 0 failed, 26 skipped (reasons listed).
- `uv run mypy`: clean (8 files).
- Smoke: `init-db` on the stale dev DB refused with the drift message (exit 1); after reset, applied 0001, re-run "up to date", 10 tables; `check-config` OK. No leftover `ecomgen_test_*` databases.

## Known failures / limitations
- Repo is in iCloud-synced `~/Documents`: iCloud hides `.venv` `.pth` files (→ `No module named 'ecomgen'`) and syncs `.env`. Verified with `UV_PROJECT_ENVIRONMENT` outside iCloud. Move to `~/dev/` pending approval.
- No CI; not run on Ubuntu; Phase 0 inventory pending.

## Next step
After PR #4: deterministic entity generation + bounded-chunk COPY, `generate --profile smoke` (1,000 orders).
