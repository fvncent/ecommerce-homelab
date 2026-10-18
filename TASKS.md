# Tasks

## Active
- [ ] PR `fix/host-postgres-tooling` (base `docs/post-merge-handoff`, PR #11): host PostgreSQL 18 + least-privilege role, no automatic drops, mount gate, digest fix; reviewed, 10 findings fixed; then merge #11 + this, tag `v0.1.1-homelab`
- [ ] Ubuntu (operator runs each step by hand): Stage 1 directories + checkout, Stage 2 role/DBs, Stage 3-5 benchmark per `docs/HOMELAB_DEPLOYMENT.md` at `v0.1.1-homelab`
- [ ] Record Ubuntu results in `docs/BENCHMARKS.md` (PR from the Mac)
- [ ] Phase 2 file feeds (CSV/JSONL + staging + manifests); its acceptance includes checking files on Ubuntu; homelab `filesystem.root` = `/srv/homelab/ecommerce/data` (spec/example still say `/srv/ecommerce-data`)

## Next (Phase 1)

## Environment / ops
- [ ] Phase 0: run `scripts/inspect_ubuntu_hardware.sh` on Ubuntu
- [ ] CI (GitHub Actions): pytest with a Postgres service + mypy; fail if integration tests skip in CI

## Deferred (later phases)
- Warehouses, inventory, shipments, returns, refunds, cart events, outbox
- Separate writer/reader DB roles; `init-db` creating the database itself
- Clearer error when `metadata_schema` is renamed after apply (currently fails loudly with DuplicateTable)

## Done
- [x] PRs #5-#10 merged into `main` (merge commits, each pinned to its reviewed head); tag `v0.1.0-homelab` -> `05bccd0`; `main` tree = tested #10 head; 139 passed / 0 skipped, mypy clean (2026-10-18)
- [x] Dev DB recreated (user-approved); smoke run + validate: 12/12 pass (2026-10-17)
- [x] PR #1 workflow rules (CLAUDE.md, handoff, .gitignore fix)
- [x] PR #2 package skeleton
- [x] PR #3 config validation + `check-config`
- [x] PR #4 migrations + `init-db` + compose Postgres; independent review, 8 findings fixed (2026-10-17)
- [x] Repo moved to `~/dev/ecommerce-homelab-planning`; local Postgres password rotated (2026-10-17)
