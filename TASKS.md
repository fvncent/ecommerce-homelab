# Tasks

## Active
- [ ] PR #4 `feat/migrations-init-db`: review fixes applied, awaiting approval to commit/push/open PR

## Next (Phase 1)
- [ ] Deterministic entity generation: seeded RNG, canonical IDs (`ord_1`, no leading zeros), FK-safe ordering
- [ ] Bounded-chunk COPY with `generation_chunks` row in the same transaction; `generate --profile smoke` (1,000 orders)
- [ ] `validate --run-id`: order header = Σ items, payments vs order total, product currency = order currency, category cycles
- [ ] `status` and `resume` (stale `RUNNING` run handling: one-running-run index blocks new runs until resolved)
- [ ] 10,000-order MVP run; record time, peak RSS, DB size

## Environment / ops
- [ ] Move repo out of iCloud `~/Documents` to `~/dev/` (needs approval; see SESSION_HANDOFF.md)
- [ ] After the move: rotate local Postgres password (`.env` was iCloud-synced)
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
- [x] Migrations + `init-db` + compose Postgres; independent review, 8 findings fixed (2026-10-17)
