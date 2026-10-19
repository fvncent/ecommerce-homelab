---
name: implement-phase
description: Development loop for one scoped ecomgen feature or fix - branch, focused reading, targeted then full tests on PostgreSQL 17 and 18, independent review, PR with green CI, TASKS/handoff updates. Use when starting or finishing any code change in this repo.
---

# implement-phase

Plan → Implement → Test → Review → GitHub CI → Approval. One bounded feature per branch.
Merges, tags, deploys and anything destructive need the user's explicit approval each time.

## 1. Start
- `git pull --ff-only` on `main`, then `git checkout -b feat/…|fix/…|chore/…`.
- First commit updates `TASKS.md` (mark merged work done, add the new item). Don't wait for an unrelated branch.
- Read only what the task touches: `docs/SESSION_HANDOFF.md` (state), the module and its tests, and the one spec doc
  CLAUDE.md maps to the topic. Use `grep -n` and `sed -n` ranges, not whole files.
- If generated rows or files can change, also follow the `data-change` skill.

## 2. Test (targeted first, then everything)
```bash
uv run mypy                                                     # strict; CI lint also runs shellcheck on scripts/*.sh
uv run pytest tests/test_<module>.py -q                         # targeted, no DB
set -a && . ./.env && set +a                                    # ECOMGEN_TEST_DATABASE_URL -> Docker PG 17 on 5433
uv run pytest tests/integration/test_<area>.py -q               # targeted, with DB
uv run pytest -q --junitxml=$SCRATCH/pg17.xml; uv run python scripts/ci_no_skips.py $SCRATCH/pg17.xml
```
For PG 18, start a throwaway container on a free port, run the suite, then remove the container:
```bash
docker run -d --rm --name ecomgen-pg18-test -e POSTGRES_USER=ecomgen -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
  -p 127.0.0.1:5434:5432 postgres:18.6-trixie
ECOMGEN_TEST_DATABASE_URL="${ECOMGEN_TEST_DATABASE_URL/5433/5434}" uv run pytest -q --junitxml=$SCRATCH/pg18.xml
uv run python scripts/ci_no_skips.py $SCRATCH/pg18.xml   # must print "N tests, 0 skipped"
docker rm -f ecomgen-pg18-test
```
- The pytest summary line is hidden by `-q` addopts: take counts from `ci_no_skips.py`, and pass/fail from the exit
  code.
- Never touch the dev DB `ecommerce_oltp`, the VLSIM containers (`vlsim-*`, port 55432) or Redpanda.

## 3. Review
- Schema/migrations, money, restart/resume, file publishing, security: `/code-review high` (or `/ponytail-review`)
  on `main...HEAD`. Fix each finding or record why it was deferred. Re-run the tests the fixes touch.
- Record the findings table in `docs/SESSION_HANDOFF.md`.

## 4. PR
- Push and open the PR only after the user approves. The PR body has summary, verification (real numbers),
  limitations, and the attribution line.
- `gh pr checks <n> --watch`. All three checks (lint, test 17.11, test 18.6) must pass, and the CI logs must show
  "0 skipped", before you ask for merge approval. Merge only on approval, with `--match-head-commit <sha>`.

## Report
Split what you **verified** (command plus actual output: counts, exit codes) from what you **did not run or
assumed**. Never report a check as passing without its output.
