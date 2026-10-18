# Session Handoff

Update before compaction or ending a significant session. Overwrite; git history keeps the old ones. Task list: `TASKS.md`.

**Updated:** 2026-10-18
**Branch:** `chore/ci`, stacked on `docs/ubuntu-benchmark` (PR #13, docs: Ubuntu results). `main` = `8c581ad`,
tagged **`v0.1.1-homelab`** (annotated; the commit verified on Ubuntu). `v0.1.0-homelab` = old container tooling.
**Deployed:** `8c581ad` on Ubuntu, clone `/srv/homelab/projects/ecommerce-homelab`. **Ubuntu verification passed**
(2026-10-18): smoke, 10k (1.78 s, 5,618 orders/s, data identical to macOS), crash/resume; snapshot diff = only our
role + 3 databases. Details: `docs/BENCHMARKS.md`.

## This task: CI (`chore/ci`)
- `.github/workflows/ci.yml`: `lint` (uv sync --frozen, mypy, `bash -n` + `shellcheck -S warning` on scripts) and
  `test` (matrix postgres 17.11-bookworm / 18.6-trixie service; `ECOMGEN_TEST_DATABASE_URL` = service superuser;
  pytest with JUnit, then `scripts/ci_no_skips.py` fails on any skip or zero tests). `permissions: contents: read`,
  `persist-credentials: false`, concurrency cancels superseded runs. Runs on every PR (any base) and push to main.
- Pinned: actions/checkout v7.0.1, astral-sh/setup-uv v10.2.0 (v10.3.0 skipped: 9 days old), uv 0.12.13 (= local),
  runner ubuntu-24.04.
- shellcheck findings at warning level: unchecked `cd` in both scripts → `|| exit 2`. Info-level notes ignored.
- Local checks: actionlint 1.7.7 clean; shellcheck 0.10.0 clean; suite with only `ECOMGEN_TEST_DATABASE_URL`
  set: 146 passed (= CI env); gate exits 0 on that report and 1 on a no-DB report (73 skipped); 5 gate unit tests.

## Ubuntu facts (from the user, 2026-10-18)
- Ubuntu Server, Ryzen 5 5600, ~14 GiB usable RAM, user `serveradmin`. Root fs ~73 GB free.
- NVMe Samsung 990 EVO Plus 1 TB, `/dev/nvme0n1p1` ext4 at `/srv/homelab` (~906 GiB free). Project root
  `/srv/homelab/ecommerce/{source,data/{csv,jsonl,parquet,events},checkpoints,logs,benchmarks,metadata}`.
- Host PostgreSQL 18 (`18/main`, 127.0.0.1:5432; only `postgres` DB). Approved databases: `ecommerce_homelab_smoke`,
  `_test`, `_crash`; dedicated least-privilege role. Never drop automatically; never change server settings.
- Do not touch: VLSIM engine + TMS, PostgreSQL 17 container (host port 55432), Redpanda. No new PG container.
- Repo is now **public** (history scanned: no secrets ever committed). Clone: `/srv/homelab/projects/ecommerce-homelab`
  (`.env` mode 600 there, `.venv` via uv). Role `ecomgen_app` + 3 databases exist and hold the benchmark data.
- User runs every Ubuntu command by hand; they prefer one complete ordered list of steps (each marked read-only or
  changes, with expected output) over one-step-per-message. Claude never SSHes.

## Previous task: host-PostgreSQL tooling (PR #12, merged)
- `homelab_bench.py`: fixed set of three databases; no create/drop; `preflight` refuses a role with any rol*
  privilege, any role membership, ownership of other databases, or non-empty databases; `same-server` (backend PID
  in the systemd unit cgroup / container); `leftovers` (client backends + prepared xacts after SIGKILL); digest
  pinned to UTC + `COLLATE "C"`; `?dbname=` (even empty) refused; `pg-stats` survives an unreachable server.
- `homelab_benchmark.sh`: OUT must be under DATA_ROOT and on `MOUNT_TARGET` with `MOUNT_UUID`, read-write (required
  on Linux); `PG_SERVICE` required on Linux and proven to be the URL's server; optional `PG_MAJOR`; throughput.
- `host_snapshot.sh`: default `/srv/homelab` minus the project dir, full depth + unreadable-path count, container
  IDs/start times/restarts, `DOCKER UNAVAILABLE` warning, `PG_LOCAL_SUPERUSER=1` (read-only `sudo -u postgres psql`:
  roles, memberships, all settings, DB ACLs, sizes/counters; works before our role exists).
- `expected_mvp_digest.txt` recomputed (same data, new method). Runbook rewritten (`docs/HOMELAB_DEPLOYMENT.md`).
- Rehearsals: Docker PG 17.11 (test DB `LOCALE 'C'` + New York time zone) and a throwaway `postgres:18` (18.6): all
  expectations passed, digest = reference; refusals tested (superuser, membership, extra owned DB, empty `?dbname=`,
  wrong server, OUT outside DATA_ROOT, wrong major, missing dir); mount gate tested in an Ubuntu container (no UUID,
  not on mount, wrong UUID; the pass case needs real ext4). Runbook Stage 2 SQL run verbatim on PG 18. All
  rehearsal roles/databases/containers removed; Mac back to `ecommerce_mvp`, `ecommerce_oltp`, role `ecomgen`.
- Tests: 146 passed with DB; 73 passed / 73 skipped without; mypy clean.

## PR #12 review (`/code-review high`) — all 10 fixed
| # | Finding | Fix |
|---|---|---|
| 1 | Handoff still named v0.1.0-homelab as target | Handoff rewritten; deploy target v0.1.1-homelab |
| 2 | Preflight ignored role memberships | Refuses any membership and ownership of other DBs; tested on PG 18 |
| 3 | Empty `?dbname=` passed | `keep_blank_values=True`; test |
| 4 | Snapshot as ecomgen_app can't precede Stage 2 | `PG_LOCAL_SUPERUSER=1` mode; before snapshot moved ahead of Stage 2 |
| 5 | Runbook put the password in an export | Snapshot needs no URL; benchmark reads `.env` |
| 6 | maxdepth 4 hid deeper changes | Full depth by default; unreadable paths counted |
| 7 | Mount gate checked DATA_ROOT, not OUT | OUT must be under DATA_ROOT; findmnt on OUT's parent |
| 8 | PG_SERVICE not tied to the URL's server | `same-server` cgroup/container check; PG_SERVICE required on Linux |
| 9 | leftovers counted autovacuum workers | `backend_type = 'client backend'` |
| 10 | Settings proof incomplete as unprivileged role | Superuser snapshot reads all settings |

## Key decisions
- Safety on the homelab rests on PostgreSQL permissions: the role can only connect to and write its own three
  databases; the script refuses to run if the role has more. Cleanup = manual, approved superuser SQL.
- Integration tests need CREATEDB (create/drop throwaway DBs) → not run on Ubuntu; Mac (and future CI) cover them.
- Server memory on Ubuntu = whole `postgresql@18-main` unit incl. page cache; WAL is server-wide.
- Earlier decisions (resume rebuilds the World from `resume_fields`, DATA_VERSION/golden, status read-only, exit
  codes) are unchanged: see git history of this file (commit `4e356de`).

## Known failures / limitations
- No CI. Mount gate's pass path and systemd cgroup path untested until Ubuntu.
- Snapshot can't see inside paths `serveradmin` can't read (counted), or docker if not permitted (warned).
- Phase 2 file feeds not implemented: `data/` stays empty; `filesystem.root` default still `/srv/ecommerce-data`.

## Next step
Push + PR `chore/ci` (base `docs/ubuntu-benchmark`) → first CI run must be green on both PostgreSQL versions (fix
on the branch if not) → user merges #13 then this → next roadmap item: Phase 2 file feeds (CSV/JSONL + manifests;
homelab root `/srv/homelab/ecommerce/data`). Re-running the Ubuntu benchmark needs the three databases recreated
by hand (approved cleanup, runbook).
