# Tasks

## Active
- [ ] PR #5 `feat/deterministic-generation`: pure generator module + tests; reviewed, 8 findings fixed; awaiting merge

## Next (Phase 1)
- [ ] Bounded-chunk COPY with `generation_chunks` row in the same transaction; `generate --profile smoke` (1,000 orders)
- [ ] `validate --run-id`: order header = Σ items, payments vs order total, product currency = order currency, category cycles
- [ ] `status` and `resume` (stale `RUNNING` run handling: one-running-run index blocks new runs until resolved)
- [ ] 10,000-order MVP run; record time, peak RSS, DB size

## Environment / ops
- [ ] Phase 0: run `scripts/inspect_ubuntu_hardware.sh` on Ubuntu
- [ ] CI (GitHub Actions): pytest with a Postgres service + mypy; fail if integration tests skip in CI

## Deferred (later phases)
- Warehouses, inventory, shipments, returns, refunds, cart events, outbox
- Separate writer/reader DB roles; `init-db` creating the database itself
- Clearer error when `metadata_schema` is renamed after apply (currently fails loudly with DuplicateTable)

## Done
- [x] PR #1 workflow rules (CLAUDE.md, handoff, .gitignore fix)
- [x] PR #2 package skeleton
- [x] PR #3 config validation + `check-config`
- [x] PR #4 migrations + `init-db` + compose Postgres; independent review, 8 findings fixed (2026-10-17)
- [x] Repo moved to `~/dev/ecommerce-homelab-planning`; local Postgres password rotated (2026-10-17)
