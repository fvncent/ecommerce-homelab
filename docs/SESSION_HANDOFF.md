# Session Handoff

Update before compaction or ending a significant session. Overwrite; git history keeps the old ones. Task list: `TASKS.md`.

**Updated:** 2026-10-17
**Branch / commit:** `feat/validate` — PR #7 open, base `feat/copy-loader-smoke` (PR #6, base `feat/deterministic-generation` = PR #5).
PR #5 still **OPEN** on GitHub (checked repeatedly; never assume merged). Merge order #5 → #6 → #7; none merged yet (user: don't merge). After GitHub confirms #5 merged: `gh pr edit 6 --base main`; after #6: `gh pr edit 7 --base main`.

## Current phase and task
Phase 1 (CLI + schema MVP) — task 6: `ecomgen validate CONFIG [--run-id RUN]`. Reviewed (`/code-review high`), 10 findings fixed; PR #7 open.

## Completed
- PRs #1–#4 merged; #5 (generation) and #6 (COPY loader + ledger + `generate --profile smoke`) open, both reviewed + fixed.
- This task: `src/ecomgen/validate.py` (`validate`, `Check`, `Result`), `validate` CLI command.

## Key decisions
- Each check = SQL returning one `id` per violation; the wrapper returns the violation count + 5 sample IDs (bounded memory/output).
- All checks in one `REPEATABLE READ, READ ONLY` transaction (consistent snapshot).
- Data checks cover the whole application schema (it holds exactly one run's data); ledger checks are per run. Default run = latest.
- Rules: header = Σ items (and every order has items); line/attempt numbers 1..n; item product currency = order currency; payment amount = order total (no partials in MVP); ≤1 non-FAILED attempt and it's the last; status↔live attempt (CREATED: none/INITIATED; CONFIRMED/PROCESSING: AUTHORIZED; PARTIALLY_SHIPPED/SHIPPED/DELIVERED: CAPTURED/SETTLED; CANCELLED: no live attempt (none, or all FAILED — docs/03 allows cancelling from CREATED) or VOIDED; NULL = violation); timestamp ordering; acyclic categories (bounded recursive CTE); run COMPLETED; ledger entities = planned entities, rows match, chunk numbers contiguous; ledger `table_rows` totals = table counts.
- Count and samples are separate queries (no full window sort); samples are the lowest IDs in numeric order.
- Exit 0 all pass; 1 a check failed; 2 config error; 3 could not validate. Requires a connection with no open transaction.
- Shared helpers `db.q` (schema formatting) and `db.has_ledger` (quoted to_regclass) used by load + validate.

## Review (`/code-review high`) — all fixed
| # | Finding | Fix |
|---|---|---|
| 1 | PARTIALLY_SHIPPED always flagged (`ELSE true`) | Same rule as SHIPPED; test |
| 2 | Status rules too loose (CONFIRMED+SETTLED, CANCELLED+INITIATED/no attempts passed) | Rules on the live attempt; 6 inconsistent-pair tests + edge cases (found a NULL bug, fixed with coalesce) |
| 3 | `SET TRANSACTION` fails on a connection with an open transaction | Refuse non-idle connections; test |
| 4 | Corruption tests didn't assert "exactly one" check fails | `== {check}`; precise corruptions; every check has one |
| 5 | Ledger entities not in `planned` were ignored | FULL JOIN; test |
| 6 | Pre-0002 skip path was dead code (unreleased) | Removed; `violations` is a plain int |
| 7 | Duplicated schema formatting / schema-exists check | `db.q`, `db.has_ledger` |
| 8 | `count(*) OVER ()` + sort of all violations; lexical samples | Separate count + `LIMIT 5` ordered by length, text |
| 9 | Exit 1 for both bad data and couldn't-validate | Exit 3 for operational errors |
| 10 | `--run-id` scopes only ledger checks, undocumented | Module docstring, CLI help, README |

## Files modified (this task)
`src/ecomgen/{validate,cli,db,load}.py`, `tests/integration/test_validate.py`, `README.md`, `TASKS.md`, this file.

## Test results (macOS, Python 3.12.14, Docker postgres:17.11)
- With DB: **112 passed, 0 failed, 0 skipped** (~15s: each validate test generates a smoke run). Without DB: 49 passed, 63 skipped (listed). mypy clean (15 files).
- Each of the 12 checks has a corruption test (bypassing schema constraints) asserting exactly that check fails; clean run → all pass.
- Dev DB `ecommerce_oltp` (local Docker, 127.0.0.1:5433): recreated with user approval (old pre-0002 smoke run dropped). `init-db` applied 0001+0002; `generate --profile smoke` → `run_20261017T143356Z_a54aba`, 5 chunks, 0.2s, 43 MB peak RSS, same row counts as before (deterministic); `validate` 12 passed, 0 failed.
- Business rule check: docs/03 allows "optional cancellation from eligible earlier states" (incl. CREATED) → CANCELLED with no payment attempt is valid; validator rule relaxed + test. CANCELLED with a pending INITIATED attempt is still rejected (judgment call; revisit if docs say otherwise).
- CI: no GitHub Actions workflows exist; `gh pr checks 7` → no checks.

## Known failures / limitations
- No regeneration comparison (DB rows vs re-generated chunks) — would catch silent value corruption the rules can't; candidate for later.
- No `resume`/`status`/reset yet; `workers` unused.
- No CI; not run on Ubuntu; Phase 0 inventory pending.

## Next step
`status` + `resume` (RUNNING/FAILED run → check `config_sha256`, rebuild world from `counts.planned`, `write_chunks`, complete).
