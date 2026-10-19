# CLAUDE.md

Synthetic e-commerce **source-data generator** (Python + PostgreSQL + files) for a homelab. It is NOT the downstream ETL/warehouse; the user builds that separately. Status and next step: `docs/SESSION_HANDOFF.md`.

## Environments
- Develop on macOS (VS Code + Claude Code). Ubuntu Server (Ryzen 5 5600, 16 GB) is deploy + integration-test only.
- Never touch the Ubuntu server, push, merge, or deploy without explicit approval.

## Where the spec lives (read only what the task needs)
- Scope/MVP: `docs/01_PROJECT_CHARTER.md` · Architecture: `docs/02_*`
- Tables, keys, contracts: `docs/03_*` · Phases + exit gates: `docs/04_*`
- Tests/acceptance: `docs/05_*` · Ops/Postgres/security: `docs/06_*`
- AI docs (`10_*`, `11_*`, `prompts/*AI*`) only when touching IDs/timestamps/versioning or Phase 9+.

## Non-negotiables (simplicity never overrides these)
- Money is `Decimal` / `NUMERIC`; amounts reconcile (items → order → payments). Timestamps UTC.
- PostgreSQL writes are transactional per chunk with a run/chunk ledger; re-runs are idempotent; `resume` works after a crash.
- FKs and constraints enforced in the schema, not just in Python.
- Bounded memory: stream/chunk, never materialize a whole run. Default ≤2 workers.
- DB + filesystem writes are not atomic: stage files, publish with checksummed manifests, reconcile.
- Secrets only via env vars (`.env.example` committed, `.env` never). Synthetic PII only.
- No Spark/Kafka/Airflow/Streamlit/LLM work before its roadmap phase.

## Workflow
- One bounded feature per branch (`feat/…`, `fix/…`, `chore/…`); meaningful commits; PR to `main`.
- Run the most relevant tests first, then the wider suite. Report actual output; say so when something was not run.
- Every feature: runnable code, tests, short doc update, stated limitations. Risky changes (schema, money, restart logic) get an independent review (`/ponytail-review` or `/code-review`).
- Never commit secrets, DB files, datasets, generated outputs, or session scratch.
- Inspect with targeted queries (`LIMIT`, counts, `head`); don't dump tables or huge logs.
- Project skills: `implement-phase` (dev loop, test commands), `data-change` (generated data, goldens, recovery),
  `homelab-release` (user-invoked). Bash safety hook: `.claude/hooks/` (README: limits, disabling). `main` and
  `v*-homelab` tags are protected by GitHub rulesets.
- Before compaction or ending a significant session, update `docs/SESSION_HANDOFF.md`. Unrelated task → suggest `/clear`.
