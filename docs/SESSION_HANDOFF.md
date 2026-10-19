# Session Handoff

Update before compaction or ending a significant session. Overwrite it: git history keeps the old versions (the full
per-PR review tables up to PR #23 are in commit `03ee496`). Task list: `TASKS.md`.

**Updated:** 2026-10-19
**Branch:** `feat/outbox` (from `main` `4632562` = PR #26 half-open windows merged, CI 387/0).
**Released:** `v0.3.0-homelab` → `0301e1f` (Phase 3A, 3B, session ordering, harness; tag pushed 2026-10-19).
`v0.2.0-homelab` (`de90a6a`) unchanged, kept for rollback.
**Deployed on Ubuntu:** `v0.3.0-homelab`, verified 2026-10-19 (`docs/BENCHMARKS.md`). Disposable DB
`ecomgen_v030_smoke` (role `ecomgen_practice`): smoke run in `commerce`, 9-step chain in `commerce_stepped`;
files in `data/v030-smoke`, `data/v030-stepped`; config `practice/v030-stepped.yaml`. `ecommerce_oltp` and the
benchmark DBs are **not** migrated to 0008 yet (optional, user-approved step). v0.3.0 commands on them were not
tried on Ubuntu; the Mac rehearsal showed a v0.2.0 run validating 20/20 after `init-db`.

## User's plan (2026-10-19)
1. Done: session ordering (#23), v0.3.0 + Ubuntu verification + walkthrough, half-open windows (#26).
2. `v0.3.1-homelab` → `4632562`: annotated tag created **locally only** after all checks (CI green, tag free, no
   migration, rehearsed upgrade/rollback from v0.3.0); push waits for the user's explicit OK (tags are immutable).
3. Now: Phase 3C outbox (design approved: order + payment events, one per transition, no `published_at`, no
   broker). Push / PR only on approval; release later as `v0.4.0-homelab` (new migration 0009, reviewed).
4. Then 3D (warehouses/inventory), 3E (catch-up / `--follow`).

## Upgrade facts for v0.3.0 (rehearsed on a throwaway PG 18.6 DB)
- Migration 0008 only adds things. On v0.2.0 runs, `validate` gives 20/20, every file's sha256 is unchanged, and
  `resume` of a COMPLETED run is a no-op.
- Rollback = check out `v0.2.0-homelab`: `status` and `validate` (19/19) still work. Its `init-db` refuses
  ("migrations this generator doesn't know: [8]"); that's harmless. v0.2.0 doesn't understand chains.
- DATA_VERSION 3 changes clickstream bytes only (rows unchanged). An unfinished v2 run can't be resumed with v0.3.0;
  all Ubuntu runs are COMPLETED.

## Ubuntu facts (from the user)
- Ryzen 5 5600, ~14 GiB RAM, user `serveradmin`.
- NVMe UUID `d263e3fd-041a-4070-a72a-28e01126aa6f`, ext4 at `/srv/homelab`.
- Clone `/srv/homelab/projects/ecommerce-homelab` (`.env` mode 600). Data `/srv/homelab/ecommerce/data`.
- Host PostgreSQL 18.6 on 127.0.0.1:5432 only. Leave VLSIM + TMS, its PG 17 container (55432) and Redpanda alone.
- Role `ecomgen_app` owns `ecommerce_homelab_{smoke,test,crash}`, which hold the benchmark data.
- Practice dataset: role `ecomgen_practice` (no privileges) owns `ecommerce_oltp`; config and env in
  `/srv/homelab/ecommerce/practice/` (`practice-10k.yaml`, `practice.env` mode 600); files in
  `/srv/homelab/ecommerce/data/practice`; run `run_20261018T135727Z_72e31d`.
- The user runs every command by hand, one step per message: purpose, read-only or modifying, exact commands,
  expected result. No heredocs. No `!` in double quotes. Claude never SSHes and never drops anything.

## Key decisions
- One run per schema set; an `advance` chain needs its own schema set. The clock is as-of inside
  `clock_start`..`clock_end`.
- Outbox: one append-only event per order/payment transition, same transaction as the row (data version 5).
- Step windows are `[previous_as_of, as_of)` (first step `[clock_start, as_of)`), data version 4; "as of T" =
  everything strictly before T, in rows and files alike. Data version 3 (`v0.3.0`) used `(previous, as_of]`.
- All steps together = the `generate` rows and records.
- Data changes bump DATA_VERSION and add a golden entry, never edit one (`data-change` skill).
- Homelab safety rests on PostgreSQL permissions: least-privilege roles, DBs created and dropped by hand.
- Integration tests need CREATEDB, so they don't run on Ubuntu; the Mac and CI cover them.

## Known limitations
- The first step of a chain generates every browsing session up to its `as_of`.
- Browse and order volume are nearly flat over time (no time-of-day shape).
- Replay timing depends on the run id. The Ubuntu-only gates are exercised only by the Ubuntu run.
- Not yet: Parquet, supplier/courier feeds, age-based retention, profiles above 10k, `workers: 2`.

## Harness (PR #24, merged)
Skills `implement-phase`, `data-change`, `homelab-release` (user-invoked); Bash guard hook `.claude/hooks/guard.py`
(active, `onFailure: block`; it matches text, so commands that merely mention a blocked pattern are blocked too:
edit such files with Edit, not Bash); GitHub rulesets on `main` (PR + 3 checks) and `v*-homelab` tags (immutable).

## Previous: half-open windows (PR #26, merged)
Windows `[previous, as_of)`, DATA_VERSION 4; `--to` whole seconds only; v<4 one-shot runs keep an inclusive end in
the window check; v3 chains refused by advance/resume. Review table in git history (commit `01be891`).

## This task: Phase 3C transactional outbox (`feat/outbox`)
- `generate.py`: `OutboxEvent` (event_id `obx_<aggregate_id>_<seq>`, aggregate_type/id/seq, event_type,
  schema_version `OUTBOX_SCHEMA_VERSION` 1, occurred_at, order_id, payment_id, canonical JSON payload with
  from/to, run_id set by the loader); `World.order_history(n, ends, window)` = rows + events from one timeline
  (`_Timeline.events`); `ORDER_TRANSITIONS` / `PAYMENT_TRANSITIONS`; generate_chunk adds `outbox_events`;
  `StepChunk.events` = every transition in [previous, as_of). DATA_VERSION 5 (other tables + files unchanged).
- Migration 0009: `{integration}.outbox_events`, FKs (order, payment, run), CHECKs (id form, aggregate, event
  type per seq, the state machine), unique (aggregate_id, seq), indexes, append-only triggers (UPDATE/DELETE/
  TRUNCATE refused).
- `load.py`: `OUTBOX`, `_table` (schema per table), `_stamped` (run_id), events written in the chunk transaction
  after the rows; ledger key `outbox_events`; `_require_loadable` refuses schemas without 0009.
- `validate.py`: `OUTBOX_CHECKS` (ledger, reconstructs rows, chains contiguous, in run window) for data_version
  >= 5 when the table exists; `CHAIN_FILES_SINCE = 4` (v4 chains' files still checkable). `db.q` knows
  `{integration}`; `db.has_outbox`.
- Benchmark digest includes the outbox (without run_id); `expected_mvp_digest.txt` gained one line.
- Tests: `tests/test_outbox.py` (chains valid, every transition seen, reconstruct rows, payload reconciles,
  deterministic, steps = one shot, multi-transition step, boundary, empty window),
  `tests/integration/test_outbox_db.py` (rollback leaves neither, resume once, CHECKs refuse invalid events,
  append-only, lineage + multi-transition step + repeat no-op, v4 run / no-0009 schema compatibility, payloads),
  corruption tests for each outbox check. 412 tests / 0 skipped on PG 17 and 18.6; benchmark 49/49; 10k e2e
  chain = generate incl. outbox, validate 24/24.

## Outbox review (`/code-review high` + `/ponytail-review`) — 9 fixed, 1 kept
| # | Finding | Fix |
|---|---|---|
| 1 | transition CHECK let a payload without `from`/`to` (or a non-object) through: NULL passes a CHECK | `jsonb_typeof(payload) = 'object' AND (CASE …) IS TRUE`; tests |
| 2 | reset hint / README: dropping app + meta schemas leaves the outbox, init-db then fails | "all three … application, integration and metadata schemas" |
| 3 | two schema sets sharing an integration schema: confusing DuplicateTable | `db.migrate` refuses 0009 on an integration schema that already has an outbox; test |
| 4 | a payment event's order not tied to the payment's order | UNIQUE (payment_id, order_id) on payment_attempts + composite FK; test |
| 5 | payload JSON built for events outside the window | window tested first; payloads built only for kept events (goldens unchanged) |
| 6 | `_stamped` silently dropped non-OutboxEvent rows | `cast` + unconditional `_replace` |
| 7 | `has_outbox` duplicated the regclass lookup | `has_table(..., schema)` |
| 8 | benchmark `sizes()` omitted the integration schema | lists app + integration tables |
| 9 | unused indexes (run_id, order_id) on the largest table | dropped; kept PK, UNIQUE (aggregate_id, seq), (occurred_at, event_id) |
| — | `order_at` "pass-through" | kept: public, used by tests (state at several times) |

After fixes: 417 tests / 0 skipped on PG 17 and 18.6; mypy clean; benchmark 49/49 (references unchanged since part 3).
Note for release: 0009 also adds a UNIQUE (payment_id, order_id) index to `payment_attempts` of existing schemas.

## Next step
Ask to push `feat/outbox` + open a PR (review done, 9 fixed). Separately: the user's OK to push the
local `v0.3.1-homelab` tag. Ponytail `lite`. Ubuntu: give the host-snapshot command on its own line.
