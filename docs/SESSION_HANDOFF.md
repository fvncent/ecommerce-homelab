# Session Handoff

Update before compaction or ending a significant session. Overwrite it: git history keeps the old versions (the full
per-PR review tables up to PR #23 are in commit `03ee496`). Task list: `TASKS.md`.

**Updated:** 2026-10-19
**Branch:** `fix/half-open-windows` (from `main` `9ad04f5` = PR #25 v0.3.0 Ubuntu docs merged, CI 378/0).
**Released:** `v0.3.0-homelab` → `0301e1f` (Phase 3A, 3B, session ordering, harness; tag pushed 2026-10-19).
`v0.2.0-homelab` (`de90a6a`) unchanged, kept for rollback.
**Deployed on Ubuntu:** `v0.3.0-homelab`, verified 2026-10-19 (`docs/BENCHMARKS.md`). Disposable DB
`ecomgen_v030_smoke` (role `ecomgen_practice`): smoke run in `commerce`, 9-step chain in `commerce_stepped`;
files in `data/v030-smoke`, `data/v030-stepped`; config `practice/v030-stepped.yaml`. `ecommerce_oltp` and the
benchmark DBs are **not** migrated to 0008 yet (optional, user-approved step). v0.3.0 commands on them were not
tried on Ubuntu; the Mac rehearsal showed a v0.2.0 run validating 20/20 after `init-db`.

## User's plan (2026-10-19)
1. Done: session ordering (PR #23), `v0.3.0-homelab`, Ubuntu verification, the user's `advance` walkthrough.
2. Now: half-open windows fix (Option 1 approved): `[previous, as_of)`, DATA_VERSION 4. Push only on approval;
   then a patch release proposal `v0.3.1-homelab` (confirm migrations: none expected), tag only on approval.
3. Then: Phase 3C outbox **plan** (no implementation before the user approves it). Then 3D, 3E.

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

## This task: half-open step windows (`fix/half-open-windows`)
- Why: on Ubuntu a browsing event at exactly `2026-01-08T00:00:00Z` belonged to the step ending then but sat in
  `date=2026-01-08`, so day-aligned steps split a day and `replay-files` refused it.
- `generate.py`: `orders_by(t)` = placed before t; `_Timeline.state_at(end)` applies changes before end only;
  `order_at` refuses an order not placed before an end; DATA_VERSION 4 (comment says what changed);
  LIFECYCLE comment: every change at or before placed + LIFECYCLE_MAX.
- `feeds.py`: `in_window` / `_in_window_text` = `[previous, as_of)`; `sessions_by` = started before t
  (`bisect_left`); `browsing_events_by` counts `< as_of`; manifest `window` text `[previous_as_of, as_of)` /
  `[clock_start, as_of)`. `validate.py` WINDOW_CHECK `at >= window_end` / `at < window_start` fail. CLI help.
- One-shot output unchanged: no record of the example or golden worlds falls exactly on `clock_end`; GOLDEN[4] /
  FEED_GOLDEN[4] repeat the v3 values; benchmark references unchanged (49/49 on PG 18.6).
- Tests: `test_clock` (a change / placement at exactly as_of goes to the next step; steps through those instants
  = generate), `test_feeds` (records at a step end belong to the next step; MIDNIGHT world seed 11 with evt_4010
  at an interior midnight: each date partition from one step), integration (midnight day replays from its step;
  the midnight event moved to the old step is caught by `records_in_their_step_window`; a v3 chain is neither
  advanced nor resumed, nothing changed). All new tests fail on the old code. 384 tests / 0 skipped on PG 17 and
  18.6. 10k e2e: 31 daily steps = generate (tables, records `cd41dcb4…`), no partition shared by two steps.
- Compatibility: no migration (0008 unchanged). v3 one-shot runs unaffected; v3 chains (Ubuntu's disposable
  `stepped`, `walk`) can't be continued (`data_version differ`), validate keeps their row checks.

## Half-open windows review (`/code-review high` + `/ponytail-review`) — 6 fixed
| # | Finding | Fix |
|---|---|---|
| 1 | **Bug:** `--to` with a fraction of a second: text vs datetime window tests disagree (events in another step than rows) | `check_step` refuses non-whole-second as_of; CLI exit 2; tests |
| 2 | v2/v3 one-shot run with a settlement at exactly clock_end fails the v4 window check | `feed_windows`: window_end + 1 µs for data_version < 4; test (v3 passes, v4 flags; fails without the fix) |
| 3 | `data-change` skill described the old windows | updated |
| 4 | `step_chunk` docstring said "placed after previous" | "at or after" |
| 5 | feeds module docstring garbled / overlong | rewrapped |
| 6 | per-event strftime in the window filter | bounds computed once per step; `_in_window_text` deleted |

After fixes: 387 tests / 0 skipped on PG 17 and 18.6; mypy clean; benchmark 49/49 on unchanged references.

## Next step
Ask to push `fix/half-open-windows` and open a PR (review done, 6 fixed). After merge + green main: patch
release proposal `v0.3.1-homelab`, then the Phase 3C plan. Ponytail level `lite`.
Ubuntu step notes: give the host-snapshot command on its own line (the v0.3.0 "before" paste got garbled).
