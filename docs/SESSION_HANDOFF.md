# Session Handoff

Update before compaction or ending a significant session. Overwrite; git history keeps the old ones. Task list: `TASKS.md`.

**Updated:** 2026-10-18
**Branch:** `chore/benchmark-files` (from `main` `b619622` = PR #17 cleanup-files merged, CI green 235/0). PR C of
3: implemented, Mac rehearsals green; independent review next. No reset or deployment without approval.
**Deployed on Ubuntu:** `v0.1.1-homelab` (`8c581ad`; no files). Keep it until `v0.2.0-homelab` passes there.
**Approved (2026-10-18):** Part 2 plan (replay → cleanup → benchmark + release); user will recreate the three
Ubuntu benchmark DBs by hand; deliveries under `<data root>/deliveries/`; cleaning a run's originals makes
`validate` fail with "deleted by cleanup" (plan defaults, accepted with the plan).

## This task: PR C, benchmark with files (`docs/HOMELAB_DEPLOYMENT.md`)
- Decisions (user, 2026-10-18): publish crash in the smoke database, sequentially after the clean smoke run, from a
  clean baseline without any reset: its own schema set (`*_pc`, derived config `generator_pc.yaml` in OUT; preflight
  requires both schema sets empty). Replay cleanup: dry run unless `CLEANUP_REPLAY=1`, only for the replay this run
  created (`owned-replay`), only if every earlier check passed; list reported first (`cleanup_plan.txt`).
- Deployment rules (user): tag + SHA verified, PRs merged + CI green, localhost:5432 only (port 55432 refused in
  preflight; `EXPECTED_PG_PORT`), inventory of the 3 DBs before proposing drops, no automatic drops, NVMe mount,
  `ECOMGEN_DATA_ROOT=/srv/homelab/ecommerce/data`, everything under `/srv/homelab/ecommerce`, snapshot diff;
  approval before creating/pushing `v0.2.0-homelab`; Ubuntu commands one step at a time, wait for output.
- `homelab_bench.py`: local-only + forbidden-port + expected-port checks; `pc-config`, `run-id`, `feed-digest`,
  `feed-stats`, `verify-files` (sha256 + orphans + staging), `feed-throughput`, `publish-crash` (kill after placing
  file N, smoke DB only), `replay-crash` (test DB only), `replay-id`, `replay-originals`, `planned-replay-digest`,
  `replay-digest`, `verify-replay`, `owned-replay`, `link-test`, `feeds-enabled`.
- `homelab_benchmark.sh`: phases smoke → publish crash → MVP → feed-only throughput → load crash → replay crash →
  cleanup; `same_as` (record-once with `RECORD_REFERENCES=1`, macOS only, exit 3); `expect` runs commands in a
  subshell so SIGKILL notices go to the log.
- Found: replay timing/duplicates are seeded by the replay id (includes the run id), so they differ per run and
  can't be a cross-machine reference; the reference is the replayed originals, and deliveries are checked against
  the locally computed plan. Making the schedule run-independent would be a REPLAY_VERSION 2 change (not done).
- References: `scripts/expected_smoke_feed_digest.txt`, `expected_feed_digest.txt`, `expected_replay_originals.txt`
  (recorded on Docker PG 17; `expected_mvp_digest.txt` re-recorded identical).
- Rehearsals: Docker PG 17.11 (48/48, `CLEANUP_REPLAY=1`) and `postgres:18.6-trixie` (44/44, dry run), exit 0;
  rehearsal role/DBs/container removed afterwards.

## Previous task: PR B, `cleanup-files` (PR #17, merged) (`docs/FEEDS.md` "Cleanup")
- `cleanup.py`: `Target` (staging | replay | run), `plan` (read-only, hashes candidates: ledger-recorded,
  not-deleted files of a COMPLETED run/replay (gone ones included, changed ones kept out), or known `staging/<id>`
  dirs of runs/replays/cleanups; manifests kept; `check_root`), `token`, `run` (run lock; token must match before
  any side effect; finishes RUNNING cleanups of the same root; cleanups row → per item `_delete` (fd chain from
  root with O_NOFOLLOW, regular file, checksum + inode, unlink / fd rmtree, fsync) → one transaction: mark + note
  in details → `_finish_audit` (DB-only JSON; already-gone not counted as deleted) → COMPLETED).
- Migration 0007: `cleanups` (target, token, data_root, details, state), `deleted_at/deleted_by` on both ledgers
  with CHECKs (both or neither; manifests never deleted), partial indexes on deleted_by.
- `feeds.RUN_ID`, `replay.REPLAY_ID`, `cleanup.CLEANUP_ID` patterns shared (staging recognition).
- `validate`: deleted files → files_match_ledger "deleted by cleanup <id>". `replay`: cleaned originals refused,
  cleaned replay verify refused (named). CLI `cleanup-files` (exit 0/1/2/3; --delete needs --confirm).
- Tests: 8 unit (`tests/test_cleanup.py`, incl. a directory swapped for a symlink mid-delete), 11 integration
  (`tests/integration/test_cleanup_files.py`, incl. a real SIGKILL after the 3rd unlink before its mark → rerun:
  1 already gone, first audit finished, refused delete side-effect free, ledger == disk; pre-0007 databases).
  Suite: 235 passed with DB, 109 passed / 126 skipped without; mypy clean (29 files).

## PR B review (`/code-review high`) — all 10 fixed
| # | Finding | Fix |
|---|---|---|
| 1 | validate/replay crash on pre-0007 databases | `db.has_table("cleanups")` gates deleted_by reads; test on a 0006-shaped DB |
| 2 | TOCTOU: symlink swap between check and unlink | fd chain with O_NOFOLLOW; hash, inode check and unlink via dir fd; fd rmtree; race test |
| 3 | Already-gone counted as deleted in audit | Recorded in details with the mark; excluded from deleted_files/bytes; test |
| 4 | Pending audits finished under any root, before token check | `cleanups.data_root`; finished only for the same root, only after the token matches; test |
| 5 | Staging dirs reported as removed before removal | Recorded per dir after rmtree (`staging_dirs_removed`) |
| 6 | Changed files stay candidates; cleanup_id collisions | Changed files excluded at plan time; random cleanup_id suffix; test |
| 7 | Docs exit 3 vs missing-0007 exit 1 | Missing 0007 → RunLookupError (exit 3) |
| 8 | `--replay-id ""` traceback | `is not None` + CleanupError → exit 2; test |
| 9 | No index on deleted_by | Partial indexes in 0007 |
| 10 | Duplicated id patterns; cln_* staging "unknown" | Shared RUN_ID/REPLAY_ID/CLEANUP_ID; cleanups known |

## Earlier: PR A, `replay-files` (PR #16, merged) (`docs/FEEDS.md` "Replay")
- `replay.py`: `Params` (from/to/feeds/delay/jitter/dup pct, validated), `replay_id` (hash of run + params +
  REPLAY_VERSION), pure `schedule` (arrival = end of partition day + delay + jitter; dup 1-24 h later; both rng draws
  always made), `delivery_manifest` (plan only, reproducible), `_originals` (manifest vs ledger sha, each original vs
  manifest; all problems before any write), `replay` (run lock; COMPLETED run; same data root; copy → verify sha →
  `feeds.place_file` → `file_deliveries` row; manifest row + COMPLETED in one transaction; verify-only once done).
- `feeds.place_file` extracted from `_place` (publish uses it; messages now "recorded file", "while placing it").
- Migration 0006: `replays`, `file_deliveries`. CLI `replay-files` (exit 0/1/2/3).
- Tests: 12 unit (`tests/test_replay.py`), 16 integration (`tests/integration/test_replay_files.py`, incl. real
  SIGKILL before the 6th placement → rerun → exact plan + reproducible manifest). Suite: 216 passed with DB,
  101 passed / 115 skipped without; mypy clean (26 files).

- Crash between a file landing in place and its PostgreSQL commit (user requirement before merging PR A): real
  SIGKILL right after `place_file` returns, for the 6th delivery and for the delivery manifest (row + COMPLETED not
  committed), and the same for publishing (4th file, run manifest). The rerun/resume adopts the placed file
  (identical bytes, fsynced, no rewrite); afterwards ledger == disk exactly (no orphans, no duplicates), staging
  empty, state COMPLETED / validate clean. Also a kill just before placing (existing tests).

## PR A review (`/code-review high`) — all 10 fixed
| # | Finding | Fix |
|---|---|---|
| 1 | Staging left forever after a crash post-COMPLETED | Staging cleared before every path, verify-only included; test |
| 2 | Manifest paths unchecked (`..`, absolute) | `feeds.safe_rel` + feed-directory/partition pattern; refused before any write; tests |
| 3 | Symlinks followed (originals, targets, staging) | `feeds.no_links`; `place_file` refuses links (publish too); tests |
| 4 | Huge hours → OverflowError traceback | 0..87,600 h bounds in Params (exit 2); test |
| 5 | feeds not normalized/validated | Sorted + deduplicated; non-empty known subset; tests |
| 6 | Verify re-read originals, failed after cleanup | Verify-only before reading originals; test with a removed original |
| 7 | Unquoted to_regclass; unmigrated DB exit 1 | `db.has_table` (quoted); no ledger → RunLookupError (exit 3); test |
| 8 | `.dupN` inserted into directories | Only the file name changes; tests |
| 9 | Docs exit code for empty range; manifest counted as delivery | Docs fixed (exit 1); newly_delivered counts files only |
| 10 | Invariants only in Python | 0006: component-wise path CHECKs, replay-dir prefix, manifest-row shape, FK to published_files and to (replay, run); test. Residual: "COMPLETED has a manifest row" stays in code (one transaction) |

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
User approves the PR C plan → benchmark with files + Mac rehearsals (PG 17, PG 18) → review → PR → merge → tag
`v0.2.0-homelab` (ask) → user runs it on Ubuntu (recreate the 3 DBs by hand first) → results PR.
