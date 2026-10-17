# Session Handoff

Update before compaction or ending a significant session. Overwrite; git history keeps the old ones. Task list: `TASKS.md`.

**Updated:** 2026-10-17
**Branch / commit:** `feat/deterministic-generation` off `main` @ `58dbef5` — PR #5 open

## Current phase and task
Phase 1 (CLI + schema MVP) — task 4: deterministic entity generation (pure module, no DB writes). Reviewed (`/code-review high`), all findings fixed, PR #5 open awaiting merge.

## Completed
- PRs #1–#4 merged (workflow rules, skeleton, config validation, migrations + `init-db` + compose Postgres).
- Repo moved out of iCloud to `~/dev/ecommerce-homelab-planning`; local Postgres password rotated.
- This task: `src/ecomgen/generate.py` — `World` (from config), per-entity generators, `generate_chunk`, `generate_all` (FK-safe order: sellers, categories, customers, products, orders+items+payments).

## Key decisions
- Per-row RNG `random.Random(sha256(seed:entity:N)[:16])`: any chunk regenerates alone (resume); output independent of `chunk_rows` (tested).
- Only `rng.random()` + integer/`Decimal` math (no `exp`/`log`) so macOS and Ubuntu produce identical data.
- Fixed-stride child IDs: `itm_{(o-1)*5+line}`, `pay_{(o-1)*3+attempt}` (sparse, computable without prior orders).
- Master data created in `[clock_start-730d, clock_start)`; order N placed in time slot N (IDs increase with time).
- Orders only from ACTIVE customers and active products, chosen uniformly by rejection sampling (32 tries, then a linear scan so it always terminates); products of non-ACTIVE sellers are inactive.
- RNG draws are unconditional, so changing one attribute's rule doesn't shift every later attribute.
- Rows are NamedTuples, loaded by column name (`_fields`), never positionally.
- Lifecycle simulated with timestamps; status/payment state = latest stage ≤ `clock_end` (late orders aren't delivered yet).
- Paths: 95% fulfil (0–2 failed attempts first), 2% payment failed (3 FAILED → CANCELLED), 3% customer cancel (AUTHORIZED → VOIDED).
- Single currency USD; country only drives tax rate. Shipping 5.99 below 50.00 net, else free.
- Golden fingerprint test (`tests/test_generate.py`) catches any change to generated data.

## Review (`/code-review high`, independent forked reviewer) — all fixed
| # | Finding | Fix |
|---|---|---|
| 1 | Linear probe biased choice toward the ID after an ineligible run (docs said uniform) | Rejection sampling + fallback scan; uniformity test |
| 2 | `World` didn't validate UTC clocks, clock order, counts ≥ 1 | `__post_init__` checks; tests |
| 3 | Positional rows couple to migration column order | Insert by `_fields` column list; docstring rule for COPY |
| 4 | O(count) probe cost when eligible entities are rare | Fixed by #1 (scan only after 32 misses) |
| 5 | `chunk_rows <= 0` → ZeroDivisionError / silent empty run | GenerationError; test |
| 6 | Hard-coded vocabulary index bounds | `_choice(rng, seq)` |
| 7 | Conditional RNG draw made product attributes depend on seller status | Draw unconditionally (also in `_between`) |
| 8 | Golden comment described the wrong hash | Comment fixed |

Fixes changed generated data; golden re-pinned (no released runs affected).

## Files modified (this task)
`src/ecomgen/generate.py` (new), `tests/test_generate.py` (new), `tests/integration/test_init_db.py` (generated rows inserted into real schema), `README.md`, `TASKS.md`, this file.

## Test results (macOS, Python 3.12.14, Docker postgres:17.11)
- With DB: **73 passed, 0 failed, 0 skipped**. Without DB: 46 passed, 27 skipped (listed).
- `uv run mypy`: clean (10 files). No leftover `ecomgen_test_*` databases.
- Example config world (10k orders → 18,776 items, 12,265 payments) generates in 0.89s.

## Known failures / limitations
- Not written to the DB yet; schema compatibility proven only by the integration test's executemany.
- Uniform customer/product choice (skew is Phase 4). No PARTIALLY_SHIPPED (no shipments yet).
- Determinism relies on CPython's `random.Random` seeding/`random()`; golden test would flag a change.
- No CI; not run on Ubuntu; Phase 0 inventory pending.

## Next step
Bounded-chunk COPY + `generation_chunks` row in the same transaction; `generate --profile smoke` (1,000 orders).
