# Tasks

## Active
- [ ] `chore/mvp-run` (stacked on PR #8): 10k MVP run + restart recorded in `docs/BENCHMARKS.md`; awaiting approval to commit/PR
- [ ] Repeat the MVP run + restart on the Ubuntu server (needs approval; Phase 0 inventory first)
- [ ] PR #8 `feat/status-resume` (base `feat/validate`): `status` + `resume`; reviewed twice, all findings fixed; awaiting merge (order: #5 → #6 → #7 → #8)
- [ ] PR #7 `feat/validate` (base `feat/copy-loader-smoke`): `validate [--run-id]`; reviewed, 10 findings fixed; awaiting merge (order: #5 → #6 → #7)
- [ ] After #5 merges (GitHub-confirmed): `gh pr edit 6 --base main`; after #6 merges: retarget #7 to `main`
- [ ] PR #6 `feat/copy-loader-smoke` (stacked on PR #5): COPY loader + chunk ledger + `generate --profile smoke`; reviewed, 10 findings fixed; awaiting merge after #5
- [ ] PR #5 `feat/deterministic-generation`: pure generator module + tests; reviewed, 8 findings fixed; awaiting merge

## Next (Phase 1)

## Environment / ops
- [ ] Phase 0: run `scripts/inspect_ubuntu_hardware.sh` on Ubuntu
- [ ] CI (GitHub Actions): pytest with a Postgres service + mypy; fail if integration tests skip in CI

## Deferred (later phases)
- Warehouses, inventory, shipments, returns, refunds, cart events, outbox
- Separate writer/reader DB roles; `init-db` creating the database itself
- Clearer error when `metadata_schema` is renamed after apply (currently fails loudly with DuplicateTable)

## Done
- [x] Dev DB recreated (user-approved); smoke run + validate: 12/12 pass (2026-10-17)
- [x] PR #1 workflow rules (CLAUDE.md, handoff, .gitignore fix)
- [x] PR #2 package skeleton
- [x] PR #3 config validation + `check-config`
- [x] PR #4 migrations + `init-db` + compose Postgres; independent review, 8 findings fixed (2026-10-17)
- [x] Repo moved to `~/dev/ecommerce-homelab-planning`; local Postgres password rotated (2026-10-17)
