# Tasks

## Active
- [ ] Optional, user-approved only: `init-db` (migration 0008) on `ecommerce_oltp` / benchmark DBs; dropping the disposable `ecomgen_v030_smoke` + `data/v030-*` when no longer wanted
- [ ] Phase 3D (branch `feat/inventory`, design approved 2026-10-20): `warehouses`, `inventory`, append-only `inventory_movements` (RECEIPT / weekly REPLENISHMENT / RESERVE / RELEASE / SHIP), one warehouse per line (customer region), opening stock sized so available >= 0 as of any time, deltas in the chunk transaction, migration 0010, DATA_VERSION 6, 6 validate checks; review findings 1 (old runs' records) and 2 (reader grants for a new practice set) fixed; push / PR on approval, then release as `v0.5.0-homelab` after approval
- [ ] Follow-up (deferred, not needed for correctness): `World.stocked_at` is recomputed per order line (~0.35 s of a 9.8 s 10k generate, ~3.5%); if it matters at larger profiles, a bounded per-run cache
- [ ] 3E catch-up / `--follow` pacing
- [ ] Later: Parquet snapshots (optional), supplier/courier feeds (need warehouses, inventory, shipments)

## Next (Phase 1)

## Environment / ops

## Deferred (later phases)
- Warehouses, inventory, shipments, returns, refunds, cart events, outbox
- Separate writer/reader DB roles; `init-db` creating the database itself
- Clearer error when `metadata_schema` is renamed after apply (currently fails loudly with DuplicateTable)

## Done
- [x] PR #28 validate fix for pre-0008 schema sets (typed NULL clock columns, regression test) merged; `main` `4302a43` CI 419/0; tag `v0.4.1-homelab` → `4302a43` pushed (user-approved, code only); Ubuntu checked out by the user: `commerce` 20/20, practice 24/24 (2026-10-20)
- [x] Ubuntu v0.4.0 verification + practice runbook on the server (2026-10-19, user-run): `practice_*` schemas in `ecommerce_oltp`, 2 days identical to the Mac rehearsal, validate 24/24, read-only `ecomgen_reader`, snapshot diff clean (`docs/BENCHMARKS.md`)
- [x] PR #27 Phase 3C outbox + practice runbook merged; `main` `877c216` CI 418/0; tag `v0.4.0-homelab` → `877c216` (user-approved; upgrade/rollback from v0.3.1 rehearsed); tag `v0.3.1-homelab` → `4632562` pushed (2026-10-19)
- [x] PR #26 half-open windows `[previous, as_of)` (DATA_VERSION 4) merged at reviewed head `01be891`; `main` `4632562` CI green 387/0; upgrade/rollback from v0.3.0 rehearsed (no migration) (2026-10-19)
- [x] User's hands-on `advance` walkthrough on Ubuntu (2026-10-19): `walk` chain, one order followed across steps, files per window, replay refusal, watermark extraction matched the step counts exactly
- [x] PR #25 (v0.3.0 release + Ubuntu verification docs) merged; `main` `9ad04f5` CI green 378/0 (2026-10-19)
- [x] Ubuntu Phase 3 verification (2026-10-19, user-run): upgrade to `v0.3.0-homelab`, 0008 in disposable `ecomgen_v030_smoke`, smoke 20/20 + files identical to the reference, 9-step chain + SIGKILL/resume drill, stepped = one-shot (7 tables, settlements), snapshot diff clean (`docs/BENCHMARKS.md`)
- [x] Tag `v0.3.0-homelab` -> `0301e1f` (user-approved; checks: main CI green 378/0 on PG 17 + 18, tag new, 0008 only migration, DATA_VERSION 3); `v0.2.0-homelab` unchanged (2026-10-19)
- [x] PR #24 Claude Code harness (3 skills, Bash guard hook with `onFailure: block`, trimmed handoff) merged; `main` `0301e1f` CI green 378/0 (2026-10-19)
- [x] GitHub rulesets: `main` (PR + lint/test 17.11/test 18.6, no force-push/delete) and `v*-homelab` tags (no update/delete) (2026-10-19)
- [x] PR #23 session ordering (DATA_VERSION 3) merged at reviewed head `03ee496`; `main` `8f667d4` CI green, 311 tests / 0 skipped on PG 17 + 18 (2026-10-19)
- [x] PR #22 Phase 3B (files per clock step) merged at reviewed head `f06e130`; `main` `a01689d` CI green, 301 tests / 0 skipped on PG 17 + 18 (2026-10-18)
- [x] PR #21 Phase 3A (`advance`: clock steps, migration 0008) merged at reviewed head `ba939e2`; `main` `3e1d5a0` CI green, 276 tests / 0 skipped on PG 17 + 18 (2026-10-18)
- [x] PR #20 (generator user guide) merged; `main` `35e7278` (2026-10-18)
- [x] Operator walkthrough on Ubuntu (2026-10-18): `ecomgen_practice` + `ecommerce_oltp` created by the user; run `run_20261018T135727Z_72e31d` (10k) COMPLETED, validate 19/19; `docs/GENERATOR_USER_GUIDE.md`
- [x] PR #19 (Ubuntu v0.2.0 results) merged; `main` `fe6d478` CI green (2026-10-18)
- [x] Ubuntu Phase 2 release verification (2026-10-18): `v0.2.0-homelab` (`de90a6a`) on host PG 18.6; 3 DBs recreated by the user after inventory; benchmark 49/49 PASS, exit 0; files/rows identical to macOS references; snapshot diff = only the 3 DBs' sizes/counters; replay cleanup dry run (`docs/BENCHMARKS.md`)
- [x] Tag `v0.2.0-homelab` -> `de90a6a` (user-approved); `v0.1.1-homelab` kept at `8c581ad` (2026-10-18)
- [x] PR #18 Phase 2 part 2 C (benchmark with files) merged at reviewed head `959fce9`; `main` `de90a6a` CI green, 251 tests / 0 skipped on PG 17 + 18 (2026-10-18)
- [x] PR #17 Phase 2 part 2 B (`cleanup-files`) merged; `main` `b619622` CI green, 235 passed / 0 skipped on PG 17 + 18 (2026-10-18)
- [x] PR #16 Phase 2 part 2 A (`replay-files`) merged; `main` `1e8be96` CI green, 216 passed / 0 skipped on PG 17 + 18 (2026-10-18)
- [x] PR #15 Phase 2 part 1 (file feeds) merged; `main` `5cf4280` CI green, 184 passed / 0 skipped on PG 17 + 18 (2026-10-18)
- [x] PRs #13 (Ubuntu results) and #14 (CI) merged; `main` CI green (2026-10-18)
- [x] Phase 0 Ubuntu inspection (user, 2026-10-18): Ryzen 5 5600, ~14 GiB, NVMe 1 TB at `/srv/homelab`, host PG 18
- [x] Ubuntu homelab verification (2026-10-18): smoke, 10k benchmark, crash/resume on host PG 18.6 at `8c581ad`; all checks passed, data identical to macOS, nothing else changed (`docs/BENCHMARKS.md`)
- [x] PRs #11 (handoff) and #12 (host-PostgreSQL tooling) merged (2026-10-18)
- [x] PRs #5-#10 merged into `main` (merge commits, each pinned to its reviewed head); tag `v0.1.0-homelab` -> `05bccd0`; `main` tree = tested #10 head; 139 passed / 0 skipped, mypy clean (2026-10-18)
- [x] Dev DB recreated (user-approved); smoke run + validate: 12/12 pass (2026-10-17)
- [x] PR #1 workflow rules (CLAUDE.md, handoff, .gitignore fix)
- [x] PR #2 package skeleton
- [x] PR #3 config validation + `check-config`
- [x] PR #4 migrations + `init-db` + compose Postgres; independent review, 8 findings fixed (2026-10-17)
- [x] Repo moved to `~/dev/ecommerce-homelab-planning`; local Postgres password rotated (2026-10-17)
