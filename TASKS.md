# Tasks

## Active
- [ ] PR `chore/ci` (base `docs/ubuntu-benchmark`, PR #13): GitHub Actions — mypy, shellcheck, pytest on PG 17.11 + 18.6, fail on any skip; awaiting first green run
- [ ] Phase 2 file feeds (CSV/JSONL + staging + manifests); its acceptance includes checking files on Ubuntu; homelab `filesystem.root` = `/srv/homelab/ecommerce/data` (spec/example still say `/srv/ecommerce-data`)

## Next (Phase 1)

## Environment / ops

## Deferred (later phases)
- Warehouses, inventory, shipments, returns, refunds, cart events, outbox
- Separate writer/reader DB roles; `init-db` creating the database itself
- Clearer error when `metadata_schema` is renamed after apply (currently fails loudly with DuplicateTable)

## Done
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
