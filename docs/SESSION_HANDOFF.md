# Session Handoff

Update before compaction or ending a significant session. Overwrite it: git history keeps the old versions (the full
per-PR review tables up to PR #23 are in commit `03ee496`). Task list: `TASKS.md`.

**Updated:** 2026-10-19
**Branch:** `chore/claude-harness` (from `main` `8f667d4`).
**main:** `8f667d4` = PR #23 (browsing-session ordering, DATA_VERSION 3) merged; CI 311 tests / 0 skipped on
PG 17.11 and 18.6. Phase 3A (`advance`, migration 0008, PR #21) and 3B (files per clock step, PR #22) are merged.
**Release:** `v0.3.0-homelab` → `8f667d4` was approved, but **not pushed yet**: the classifier blocked the push and
the user was given the command. `v0.2.0-homelab` (`de90a6a`) stays as it is. The release report (migrations,
data version, checksums, upgrade/rollback rehearsal) is in the 2026-10-19 conversation. Re-create it with the
`homelab-release` skill.
**Deployed on Ubuntu:** `v0.2.0-homelab` (benchmark 49/49, 2026-10-18).

## User's plan (2026-10-19)
1. Session-ordering fix: done (PR #23).
2. Phase 3 release `v0.3.0-homelab`: tag push pending (user).
3. Ubuntu verification, run by the user one step at a time:
   - isolated smoke DB first;
   - `init-db` on persistent DBs only as separately approved steps;
   - a multi-day `advance` walkthrough in its own schema set;
   - inspect PostgreSQL, CSV and JSONL; validate;
   - update `docs/GENERATOR_USER_GUIDE.md` with the verified commands. Its `init-db` example (0001-0007) and
     "19 passed" become 0001-0008 and 20.
4. Phase 3C (outbox) only after step 3 and the user's hands-on walkthrough. Then 3D (warehouses/inventory) and 3E
   (catch-up / `--follow`).

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
- Step windows are `(previous_as_of, as_of]` (first step `[clock_start, as_of]`).
- All steps together = the `generate` rows and records.
- Data changes bump DATA_VERSION and add a golden entry, never edit one (`data-change` skill).
- Homelab safety rests on PostgreSQL permissions: least-privilege roles, DBs created and dropped by hand.
- Integration tests need CREATEDB, so they don't run on Ubuntu; the Mac and CI cover them.

## Known limitations
- The first step of a chain generates every browsing session up to its `as_of`.
- Browse and order volume are nearly flat over time (no time-of-day shape).
- Replay timing depends on the run id. The Ubuntu-only gates are exercised only by the Ubuntu run.
- Not yet: Parquet, supplier/courier feeds, age-based retention, profiles above 10k, `workers: 2`.

## This task: Claude Code harness (user approved all 4 items, 2026-10-19)
- Skills in `.claude/skills/`:
  - `implement-phase`: dev loop, test commands incl. the throwaway PG 18 container, review, PR/CI gate;
  - `data-change`: DATA_VERSION, goldens, reference re-recording, equality, recovery, compatibility;
  - `homelab-release`: report, rehearsal, tag, Ubuntu steps; `disable-model-invocation`, so the user runs it.
- `.claude/hooks/guard.py` blocks these Bash commands:
  - deleting or moving tags; force-pushing main or tags; `gh` deletes;
  - VLSIM or Redpanda docker changes; docker prune; port 55432;
  - rm, mv or find -delete under `/srv/homelab`; ssh;
  - dropping `ecommerce_oltp`, `ecomgen_practice` or the VLSIM DBs.
  It takes ~20 ms and never runs anything itself. Errors don't authorize a risky command: unparseable input is
  checked for risky words, and `onFailure: "block"` blocks the command if the script can't run.
  `tests/test_guard.py` runs it under the venv Python and macOS's `/usr/bin/python3` 3.9 (67 tests). Active via
  `.claude/settings.json` (approved 2026-10-19). Its limits and how to disable it are in `.claude/hooks/README.md`.
  It matches text, so a command that merely *mentions* a blocked pattern (a heredoc of test strings, `echo "git tag
  -d"`) is blocked too: edit such files with Edit, not Bash.
- GitHub rulesets (active):
  - 25365485 on `main`: no delete or force-push; PR required; lint plus both test checks required.
  - 25365486 on `v*-homelab` tags: no delete, update or force; creating a tag is allowed.
- Ponytail plugin loads after `/reload-plugins` (`/ponytail-review` now exists).
- This file went from 27,608 bytes to the size in the PR description.

## Next step
PR for `chore/claude-harness` is open; wait for the user's merge approval. The user has to type `/ponytail lite`
(the persisted level is `full`; only the user prompt hook changes it). The tag `v0.3.0-homelab` waits for the
user's go-ahead. Then Ubuntu step 3 with the `homelab-release` skill.
