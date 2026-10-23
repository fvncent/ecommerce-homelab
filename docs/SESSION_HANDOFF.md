# Session Handoff

Update before compaction or ending a significant session. Overwrite it: git history keeps the old versions (the full
per-PR review tables up to PR #23 are in commit `03ee496`, the outbox review in `7bfea59`). Task list: `TASKS.md`.

**Updated:** 2026-10-23
**Branch:** `feat/inventory` (from `main` `7bfea59`), push + PR approved 2026-10-23 (merge needs approval). Phase 3D, design approved 2026-10-20.
**Released:** `v0.4.1-homelab` → `4302a43` (validate fix), `v0.4.0-homelab` → `877c216` (outbox), `v0.3.1-homelab`
→ `4632562`, `v0.3.0-homelab` → `0301e1f`, `v0.2.0-homelab` → `de90a6a`. Tags are immutable (ruleset).
**Ubuntu:** on `v0.4.1-homelab` (user-run checkout 2026-10-20): old `commerce` set in `ecommerce_oltp` validates 20/20,
practice set (`practice_*`, data version 5, 2 days to 2026-01-03) 24/24. The user reads it as `ecomgen_reader`
(`~/.pgpass`; the password was pasted in chat once and rotated). VLSIM containers on Ubuntu stopped/removed by the
user; on the Mac they auto-start with Docker Desktop (left alone).

## User's plan
1. Done: session ordering (#23), v0.3.0, half-open windows (#26), outbox (#27, v0.4.0), validate fix (#28, v0.4.1),
   practice runbook, Ubuntu verification, handoff docs (#29).
2. Now: Phase 3D on `feat/inventory`: review findings 1 and 2 fixed (user-approved 2026-10-21); push / PR only on
   approval; release later as `v0.5.0-homelab`.
3. Not before 3D is reviewed and approved: 3E (catch-up / `--follow`), Phase 4.

## Phase 3D: warehouses and inventory (`feat/inventory`)
- Migration 0010: `warehouses` (`wh_N`, region NA/EU/APAC, nominal capacity), `inventory` (PK warehouse+product,
  `0 <= reserved <= on_hand`), append-only `inventory_movements` (CHECK: id form, order line and deltas per type,
  `IS TRUE`; FK to inventory, composite FK (order_item_id, product_id) via a new UNIQUE on `order_items`; index
  (occurred_at, movement_id); UPDATE/DELETE/TRUNCATE triggers). DATA_VERSION 6; other tables and files unchanged
  (guard test vs GOLDEN[5] / GOLDEN[4]).
- `generate.py`: products stocked in 1-3 consecutive warehouses (`stocked_at`), a line ships from the customer's
  region else the primary (`fulfils`, no draw from the order RNG); movements RECEIPT (before clock_start, first
  window only), weekly REPLENISHMENT (`restock_times`, `restock`), RESERVE / RELEASE / SHIP at CONFIRMED / CANCELLED
  after confirmation / SHIPPED (`_Timeline.moves`). `_openings` (one pass over every order, lru_cache 1) sizes the
  opening so available >= 0 as of any time: as of (delivery j, j+1] at most the units reserved before j+1 are out.
  `_draws` holds an order's draws (shared by `order_history` and `_openings`). Entity `inventory` (chunked by
  product) between `warehouses` and `orders`. `WORLD_FIELD_SINCE` = when each defaulted World field was added.
- `load.py`: `_insert` → `_apply_stock` (RECEIPT inserts the row; other deltas summed per cell, one UPDATE, rowcount
  checked) then the movements, same transaction; every step writes its inventory chunks (replenishments) before its
  orders; `_require_loadable` needs migration 10 in the set's `schema_migrations` (`db.has_migration`);
  `recorded_world` defaults only fields added after the run's data version.
- `validate.py`: 6 `INVENTORY_CHECKS` (ledger, inventory = sum of movements, movements follow the outbox history,
  line's warehouse (one, local when stocked locally), stock never negative as of any movement time, movements in
  run window) for data_version >= 6 with the table: 30 checks with both feeds. `status` lists only the entities a run planned or wrote.
- Bench: digest +3 lines (warehouses, inventory, movements without run_id); `digest_ok` 11; crash hook takes the
  window arg. `expected_mvp_digest.txt`: 8 lines unchanged + 3 new; feed references unchanged.
- Docs: user guide section, data model, README, practice runbook (v0.5.0, older sets stay on their version, reader
  grants for a new set).

## Phase 3D review (`/ponytail-review`) and fixes
| # | Finding | Fix |
|---|---|---|
| 1 | `validate` crashed (malformed record) on any pre-v6 run with files: `recorded_world` required the new `warehouses` | defaults only for fields added after the run's data version (`WORLD_FIELD_SINCE`); required fields still refused; unit tests + DB test (fail on the old code); real v0.4.1 one-shot + 2-day chain validate 24/24 before and after `init-db`, rows and files byte-identical, advance refused |
| 2 | runbook's "new practice set" left `ecomgen_reader` without access | grants as the set's owner (USAGE, SELECT ON ALL TABLES, ALTER DEFAULT PRIVILEGES), reader checks; `test_reader_role.py` runs the runbook's statements; rehearsed with non-superuser roles |
| 3 | `stocked_at` recomputed per line | measured ~0.35 s of a 9.8 s 10k generate (~3.5%): deferred (TASKS), no cache |

Independent passes (general-purpose agents with QA / data-reliability briefs, 2026-10-21). Data reliability: no
must-fix; non-negativity held in 35 adversarial worlds x 4 step layouts x 2 chunk sizes and a double crash + resume;
v0.4.1 datasets (git archive) behave as documented; validate at 100k orders (400k movements) 8.0 s. QA: 9 mutations.
| # | Finding | Fix |
|---|---|---|
| R1 | validate never checked a line's warehouse (moved movements + adjusted rows passed) | check `movements_from_the_lines_warehouse` (one warehouse; local when stocked locally; primary not stored, so any stocking one otherwise); corruption test |
| Q1 | `movements_in_run_window` bounds untested (mutations survived) | chain test shrinks step 3's start and step 2's end (replica mode past the chain FK) |
| Q2 | movement quantity untested in `movements_follow_order_history` | corruption: a cancelled line's RESERVE + RELEASE one unit larger |
| Q3 | `_has_rows` for the new tables untested | stray warehouse row → clean refusal, no run recorded |
| Q4 | receipt time only guarded by the golden | unit test: receipt = max(warehouse, product created_at) |
Lineage (user, 2026-10-23): the primary fallback is not stored; unit test `test_without_a_local_warehouse_a_line_falls_back_to_the_products_primary` (1/3/4 warehouses, mutant killed), user guide "Lineage" note, TASKS enhancement (stocking table + allocation reason). Stock and money don't depend on it.
Mutations re-run with pytest exit codes: all of the above killed; the split-line clause (`n > 1`) has no isolated
corruption (a split always moves reserved units, so the stock checks fire too).

Verification so far: PG 17 446/0 and PG 18.6 446/0 before the fixes; benchmark 49/49 on PG 18.6 (references
re-recorded, then verified); 10k: generate vs 31 daily steps identical in all 11 tables and both feeds, validate 29/29
each; 10k generate 9.8 s vs 6.8 s on v0.4.1 (same container).

## Ubuntu facts (from the user)
- Ryzen 5 5600, ~14 GiB RAM, user `serveradmin`. NVMe UUID `d263e3fd-041a-4070-a72a-28e01126aa6f`, ext4 at
  `/srv/homelab`. Clone `/srv/homelab/projects/ecommerce-homelab`. Data `/srv/homelab/ecommerce/data`.
- Host PostgreSQL 18.6 on 127.0.0.1:5432 only. Role `ecomgen_app` owns `ecommerce_homelab_{smoke,test,crash}`
  (benchmark). Role `ecomgen_practice` owns `ecommerce_oltp`: old `commerce` set (`practice/practice-10k.yaml`,
  `practice.env`, files `data/practice`) and the pipeline set (`practice/pipeline.yaml`, `pipeline.env`, files
  `data/practice-pipeline`). Disposable `ecomgen_v030_smoke` + `data/v030-*`, `data/walk` (drop only on request).
- The user runs every command by hand, one step per message: purpose, read-only or modifying, exact commands,
  expected result. No heredocs. No `!` in double quotes. Claude never SSHes, never drops or resets anything, never
  changes Ubuntu roles.

## Key decisions
- One run per schema set; a chain needs its own set and stays on the data version it started with.
- Windows `[previous_as_of, as_of)`; "as of T" = strictly before T; all steps together = `generate`.
- Outbox and stock movements are written in the chunk transaction of the change they record; append-only.
- Data changes bump DATA_VERSION and add a golden, never edit one (`data-change` skill).
- Homelab safety rests on PostgreSQL permissions; DBs created and dropped by hand. Integration tests need
  CREATEDB, so they run on the Mac and CI only.

## Known limitations
- One warehouse per order line (no split lines, no backorders); capacity is nominal; replenishment sized from
  expected demand, not reorder points; no inventory events in the outbox; no shipments table yet.
- `_openings` memory = cells x weeks (shortcut comment); its pass is ~1 s per 10k orders in the first step.
- First step of a chain generates every browsing session up to its as_of; flat volume over time.
- Not yet: Parquet, supplier/courier feeds, profiles above 10k, `workers: 2`.

## Harness
Skills `implement-phase`, `data-change`, `homelab-release`; Bash guard hook `.claude/hooks/guard.py` (matches text:
edit files that mention blocked patterns with Edit); rulesets on `main` (PR + lint, test 17.11, test 18.6) and
`v*-homelab` tags (immutable). "QA / Data Reliability agents" are not defined here: general-purpose agents with
those briefs stand in.

## Next step
PR for `feat/inventory`: wait for CI (lint, PG 17.11, PG 18.6, 0 skipped), report, then the user approves the merge.
Then (on approval) tag `v0.5.0-homelab` and the user-run Ubuntu upgrade. No 3E / Phase 4 before that. Ponytail `lite`.
