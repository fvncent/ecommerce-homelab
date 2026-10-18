# Tasks

## Active
- [ ] PR B `cleanup-files` (branch `feat/cleanup-files`, plan awaiting approval): dry-run default, confirm token, ledger-known files only, root containment + no symlinks, manifests/audit kept, `deleted_at` (migration 0007), validate reports deleted files
- [ ] PR C: benchmark with files (per-feed throughput, sha256 MB/s, feed digest vs macOS reference, publish-crash + replay-crash), then tag `v0.2.0-homelab`; Ubuntu: user recreates the 3 benchmark DBs by hand (approved 2026-10-18), `ECOMGEN_DATA_ROOT=/srv/homelab/ecommerce/data`; keep `v0.1.1-homelab` until it passes
- [ ] Later: Parquet snapshots (optional), supplier/courier feeds (need warehouses, inventory, shipments)

## Next (Phase 1)

## Environment / ops

## Deferred (later phases)
- Warehouses, inventory, shipments, returns, refunds, cart events, outbox
- Separate writer/reader DB roles; `init-db` creating the database itself
- Clearer error when `metadata_schema` is renamed after apply (currently fails loudly with DuplicateTable)

## Done
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
