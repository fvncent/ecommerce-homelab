# Session Handoff

Update before compaction or ending a significant session. Overwrite; git history keeps the old ones. Task list: `TASKS.md`.

**Updated:** 2026-10-17
**Branch / commit:** `feat/copy-loader-smoke`, stacked on `feat/deterministic-generation` (`0803f94`, PR #5 **still open**) — uncommitted. Rebase onto `main` after #5 merges.

## Current phase and task
Phase 1 (CLI + schema MVP) — task 5: bounded-chunk COPY loader + run/chunk ledger + `ecomgen generate [--profile smoke]`. Reviewed (`/code-review high`), 10 findings fixed; needs approval to commit/push/PR.

## Completed
- PRs #1–#4 merged; PR #5 (deterministic generation, reviewed, 8 findings fixed) open.
- This task: `src/ecomgen/load.py` (`run`, `write_chunks`, `world_for`, `PROFILE_COUNTS`), `generate` CLI command; integration fixtures moved to `tests/integration/conftest.py` (`new_db` factory for multi-DB tests).

## Key decisions
- One transaction per chunk: COPY rows (orders chunk = orders, items, payments) + `generation_chunks` marker. Marker present ⇔ chunk durable.
- `write_chunks` skips chunks already marked for the run → re-running a run is idempotent; `resume` will call it with the existing run ID.
- `generate` refuses any DB that already has a run or source rows (deterministic IDs would collide); message gives the reset steps until `resume` exists.
- Error → run `FAILED` with `error`; lost connection / hard crash → stays `RUNNING` (resume treats both).
- Session advisory lock for the whole run: one generate/resume at a time. Loader requires an autocommit connection (otherwise `conn.transaction()` would be a savepoint).
- Rows loaded by column name (`_fields`), never positionally. `bulk_copy: false` uses executemany INSERT (tested to give identical data).
- `--profile smoke` = 20 sellers, 500 customers, 200 products, 1,000 orders, only when the flag is given; otherwise config counts (config `profile` is just a label).
- Run `counts` jsonb = `{planned: {entity: n}, written: {table: n}}`; `planned` lets resume rebuild the world (also check `config_sha256`); `written` summed from the ledger's per-chunk `table_rows` (migration 0002) in the same UPDATE that marks COMPLETED — no `count(*)` scans.
- Seed capped at 2^63-1 in config (bigint column).

## Review (`/code-review high`) — all fixed
| # | Finding | Fix |
|---|---|---|
| 1 | After any failure `generate` is blocked and points to a nonexistent `resume` | Message gives concrete reset steps; `resume` is the next task |
| 2 | Config `profile: smoke` silently overrode config counts | Preset only with explicit `--profile`; test |
| 3 | Seed > bigint passed config, failed at insert | Config caps seed at 2^63-1; test |
| 4 | Test digest `string_agg(... ORDER BY 1)` ordered by a constant | `ORDER BY x::text` |
| 5 | Full `count(*)` on every table at run end | Totals from ledger |
| 6 | Ledger lacked child-table counts per chunk | Migration 0002 `table_rows jsonb`; test vs actual counts |
| 7 | Error after all chunks durable → FAILED | Completion + totals in one UPDATE (window minimal); resume will recover FAILED |
| 8 | Guard ignored source rows not recorded by a run | Also refuses non-empty source tables; test |
| 9 | Old hand-rolled INSERT test duplicated loader | Removed (loader tests cover schema conformance) |
| 10 | `migrated()` helper duplicated conftest setup | `migrated` factory fixture in conftest |

## Files modified (this task)
`src/ecomgen/{load,cli,config}.py`, `src/ecomgen/migrations/0002_chunk_table_rows.sql`, `tests/test_config.py`, `tests/integration/{conftest,test_load,test_init_db}.py`, `tests/test_cli.py`, `README.md`, `TASKS.md`, this file.

## Test results (macOS, Python 3.12.14, Docker postgres:17.11)
- With DB: **85 passed, 0 failed, 0 skipped**. Without DB: 49 passed, 36 skipped (listed). `uv run mypy`: clean (13 files). No leftover test DBs.
- Covered: CLI smoke end-to-end + second run refused; ledger sums = table counts; T02 (two clean DBs, different chunk sizes → identical md5 per table); COPY = INSERT path; mid-run CHECK failure → only whole chunks committed, run FAILED, re-run skips 7 / writes 2 → identical to clean run, then 0 written; uninitialized DB, concurrent lock, non-autocommit refused.
- Smoke on dev DB: 5 chunks in 0.3s, 43 MB peak RSS; reconciliation SQL 0 errors. **Dev DB now holds that smoke run** (pre-0002, so its chunk rows have `table_rows = {}`); 0002 applied to it cleanly.
- 10k-order config run (throwaway DB, before review fixes): 6 chunks, 1.9s, 63 MB peak RSS, 19 MB DB.

## Known failures / limitations
- No `resume`/`status`/`validate` commands yet; `workers` unused (single process).
- No reset command: to re-generate, drop/recreate the dev DB volume or schemas manually.
- Uniform choice, USD only, no PARTIALLY_SHIPPED (see PR #5).
- No CI; not run on Ubuntu; Phase 0 inventory pending.

## Next step
`validate --run-id`, then `status` + `resume` (RUNNING/FAILED run → `write_chunks`, check `config_sha256`).
