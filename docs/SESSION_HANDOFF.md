# Session Handoff

Update before compaction or ending a significant session. Overwrite; git history keeps the old ones. Task list: `TASKS.md`.

**Updated:** 2026-10-18
**Branch:** `feat/file-feeds` (from `main` `d3ff25a`; PRs #13 + #14 merged, CI green on main). Not pushed yet.
**Deployed on Ubuntu:** `v0.1.1-homelab` (`8c581ad`), verified 2026-10-18 (`docs/BENCHMARKS.md`). The new code needs
`ECOMGEN_DATA_ROOT` there (part 2 updates the benchmark).

## This task: Phase 2 part 1, file feeds (`docs/FEEDS.md`)
- `feeds.py`: payment_settlement CSV (SETTLED payments; fee = round_half_up(gross*pct/100 + fixed), capped; net =
  gross - fee) and clickstream JSONL (`clickstream_events` browsing events in sessions of 5 + one `order_placed` per
  order), from the same World as the DB rows; date partitions; rollover at max_file_size_mb; bounded entity cache.
- Publish: staging/<run> → fsync → mkdirs with parent fsyncs → `os.link` (no replace) → dir fsync → ledger row
  (`published_files`, migration 0005); manifest last (built from DB only: deterministic); run COMPLETED after.
  Repeat/resume: recorded files must match; unrecorded identical file adopted (after fsync); else FeedError; once
  the manifest is recorded, verify-only (checksums + manifest lists exactly the ledger files).
- `load`: `_execute` = chunks → publish → complete; `resume_fields` records `data_root` (only if a feed is on) and
  `feeds` (enabled + max_file_size_mb), both in RESUME_MUST_MATCH; smoke preset has 10,000 browsing events.
- `config`: `ECOMGEN_DATA_ROOT` override (`config.data_root`); parquet=true and publish_via_staging=false refused.
- `generate.DATA_VERSION = 2`, `World.clickstream_events` (default 0); GOLDEN[2] = GOLDEN[1] (DB rows unchanged);
  `FEED_GOLDEN[2]` in tests/test_feeds.py.
- `validate`: temp tables created before the RR READ ONLY snapshot (CREATE is refused inside it, COPY into temp
  tables works), file + feed checks inside it, dropped after; per-feed gating; NULL-safe; unreadable → violation.
  19 checks for a run with both feeds. DB-corruption tests run with feeds off (one check each); feed corruptions in
  `tests/integration/test_publish.py` (caught by their check, others may also fire).
- CLI: files summary; config error (exit 2) for a bad `ECOMGEN_DATA_ROOT` only in generate/resume; OSError → exit 1
  with a status hint (validate: exit 3). Removed a duplicated `elif` in status.
- Tests: 184 passed with DB, 89 passed / 95 skipped without; mypy clean (23 files). Includes a real SIGKILL during
  publishing (4th file) → resume → identical to a clean run.

## Review (`/code-review high`) — all 10 fixed
| # | Finding | Fix |
|---|---|---|
| 1 | All feed checks ran when only one feed was published | Checks gated per published feed; single-feed integration tests |
| 2 | New directories / adopted files not fsynced | `_mkdirs` fsyncs parents; adoption fsyncs file + dir chain |
| 3 | NULL fields passed settlement checks | IS DISTINCT FROM + explicit NULL checks; malformed-event check; test |
| 4 | File checks outside the DB snapshot | Temp tables pre-created; everything in one RR READ ONLY snapshot |
| 5 | OSError/csv.Error crashed validate | Reported as files_match_ledger violations; CLI exit 3 for others; test |
| 6 | exists() + os.replace could overwrite | `os.link` (EEXIST → FeedError); race test |
| 7 | Misleading "run is FAILED" for pre-run OSError | Message conditional, points to status |
| 8 | Resume restaged everything after full publish | Verify-only fast path once the manifest is recorded |
| 9 | data_root recorded/validated with feeds off | Recorded only with feeds; only generate/resume validate it; status reports instead of crashing |
| 10 | Unused field, mixed timestamp formats | `_Writer.run_id` removed; `published_at` via `ts()`; FEEDS.md documents it |

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
User approves push + PR of `feat/file-feeds` → CI green → merge → Phase 2 part 2 (replay-files, retention,
benchmark + Ubuntu re-run with `ECOMGEN_DATA_ROOT=/srv/homelab/ecommerce/data`, new tag). Mac dev `.env` needs
`ECOMGEN_DATA_ROOT` pointing at an existing directory before `generate` against `ecommerce_oltp`-style DBs.
