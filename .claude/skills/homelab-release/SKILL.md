---
name: homelab-release
description: Prepare a versioned ecomgen homelab release and guide its Ubuntu verification - release report, upgrade/rollback rehearsal on a throwaway database, tag only after approval, Ubuntu commands one step at a time run by the user. Use for "prepare the release", "tag", "deploy to Ubuntu", "Ubuntu verification".
disable-model-invocation: true
---

# homelab-release

The user runs every Ubuntu command by hand. Claude never SSHes to the server, never deploys, never drops anything
and never moves or deletes an existing tag. A new release gets a new tag `vX.Y.Z-homelab`.

## 1. Preconditions (verify; don't assume)
- Every PR for the release is merged. `gh run list --branch main --limit 1` shows success, and the CI logs show
  "N tests, 0 skipped" on PG 17.11 and 18.6.
- `git fetch --tags`. The new tag name must not exist (`git ls-remote --tags origin`).

## 2. Release report (before tagging)
- **Version and SHA:** the exact full commit SHA (`git rev-parse main`), and the PRs included
  (`git log --oneline <last-tag>..main --merges`).
- **Migrations:** `git diff --stat <last-tag>..main -- src/ecomgen/migrations`. For each one: add-only? Does it
  rewrite data? Its sha256.
- **Data version:** DATA_VERSION before and after; which rows or files change; what old runs can still do.
- **Checksums:** `shasum -a 256 scripts/expected_*.txt`, marking which changed.
- **Config changes:** `git diff <last-tag>..main -- config/ .env.example`.
- **Rehearsal** in a throwaway PG 18.6 container, with the last tag in a `git worktree` and its own `uv sync`:
  1. The old release runs `init-db`, `generate`, `validate`.
  2. The new release runs `init-db` (only the new migrations apply), `status`, `validate` of the old run, and
     `resume` (expect a no-op or a clear refusal). Run sha256 over all files before and after; they must be equal.
  3. The old release, against the migrated DB, runs `status` and `validate`.
  4. Remove the container and the worktree afterwards.
- **Upgrade plan:** the ordered Ubuntu steps (see §4).
- **Rollback plan:** check out the old tag and `uv sync --frozen`; say what the old release can and can't do on a
  migrated DB.
- Ask for approval to tag. Then
  `git tag -a <tag> <sha> -m "…"` and `git push origin <tag>`. If a permission rule blocks the push, give the
  user the exact `! git …` command instead.

## 3. Ubuntu facts
- Clone: `/srv/homelab/projects/ecommerce-homelab`. Data: `/srv/homelab/ecommerce/data`. Practice config and env:
  `/srv/homelab/ecommerce/practice/`.
- NVMe UUID `d263e3fd-041a-4070-a72a-28e01126aa6f` must be mounted at `/srv/homelab`.
- PostgreSQL 18 on `127.0.0.1:5432` only. Leave VLSIM, its PG on 55432, and Redpanda alone.
- Persistent DBs (`ecommerce_oltp`, benchmark DBs) are never reset. `init-db` on them is its own approved step.
  The user creates and drops DBs by hand.

## 4. Ubuntu steps (one step per message; wait for the user's output)
For each step give: the purpose, read-only or modifying, the exact commands, and the expected result.
Shell rules:
- No heredocs.
- No `!` inside double quotes (bash history expansion); use `NOT LIKE 'pg\_%'` instead.
- Source env files as `set -a; . /path/file.env; set +a`.

Order:
1. Read-only checks: mount, port 5432 bound to localhost, disk, VLSIM up. Then
   `scripts/host_snapshot.sh` to `before-<tag>.txt`.
2. `git fetch --tags && git checkout <tag> && uv sync --frozen`; `git describe --tags` must equal the tag.
3. Disposable smoke DB (user-created): `init-db`, `generate --profile smoke`, `validate`.
4. Optional, only when approved: `init-db` on persistent DBs; the benchmark (needs its 3 DBs empty; the user
   recreates them).
5. Feature walkthrough (for example a multi-day `advance` in its own schema set). Then take the after snapshot
   and diff it.

Afterwards, record the verified Ubuntu results in `docs/BENCHMARKS.md`, `TASKS.md` and the handoff, with the
actual output, and update `docs/GENERATOR_USER_GUIDE.md` with commands that were actually verified.
