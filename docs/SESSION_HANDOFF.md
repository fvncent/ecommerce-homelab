# Session Handoff

Update before compaction or ending a significant session. Overwrite; git history keeps the old ones.

**Updated:** 2026-10-17
**Branch / commit:** `feat/config-validation` (see `git log`)

## Current phase and task
Phase 1 (CLI + schema MVP) — task 2 done: config loading + validation.

## Completed
- PR #1 (workflow rules) and PR #2 (package skeleton) merged.
- `src/ecomgen/config.py`: strict validation of `generator`, `postgres`, `filesystem` (types, ranges, enums, UTC chronology, SQL identifiers, distinct schemas, unknown-key rejection with dotted paths); config SHA-256 of raw bytes; DB URL resolved from env var without leaking it.
- `ecomgen check-config CONFIG` (exit 2 on config or env error).

## Key decisions
- Validation hand-written on frozen dataclasses (no pydantic). PyYAML `safe_load` is the only runtime dep.
- `scenario`, `ai_readiness`, `safety`, `logging` accepted unvalidated until their phase; any other unknown top-level key is rejected.
- `workers` capped at 2 until Phase 7 profiling. `profile` is a label only; counts come from the file (no presets).
- Output dir existence/writability is checked at generate time, not in config (the Mac has no `/srv`).
- `.env` not auto-loaded (no python-dotenv); README shows `set -a; . ./.env`.

## Files modified
`src/ecomgen/{config,cli}.py`, `tests/test_config.py`, `pyproject.toml`, `uv.lock`, `config/generator.example.yaml` (header comment), `README.md`, this file.

## Test results (macOS, Python 3.12.14)
- `uv run pytest`: 26 passed. `uv run mypy`: clean (6 files).
- Manual: `check-config` exits 2 without `ECOMMERCE_DATABASE_URL`, 0 with it.

## Known failures / limitations
- Not run on Ubuntu; Phase 0 hardware inventory pending. No CI.
- "Seeded clock" from roadmap task 2 deferred to generation, where it is used.
- `*.jsonl` ignored globally; fixtures will need a `!tests/fixtures/**` exception.

## Next step
Phase 1 task 3: SQL migrations (schemas `commerce` / `integration` / `generator_meta`, core tables with FKs/CHECKs, run+chunk ledger) and `ecomgen init-db`, tested against a local PostgreSQL.
