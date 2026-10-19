# Benchmarks

Measured runs, not promises (charter: record time, peak RSS, WAL growth, footprint and CPU per completed run).
Add a row per run; note the environment. The target is the Ubuntu server; macOS rows are development baselines.

## Runs

| Date | Env | Commit | Profile | Orders / items / payments | Chunks | Wall | CPU user+sys | Peak RSS | WAL | DB size | validate |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-18 | **Ubuntu homelab**, same host, **with files** (120 files, 31.6 MB) | `de90a6a` (`v0.2.0-homelab`) | mvp (config counts, `clickstream_events: 100000`) | 10,000 / 18,776 / 12,265 | 6 | 4.62 s (DB + files) | 3.44 + 0.11 s | 68 MB | 16.6 MB | 20.0 MB | pass, 1.35 s |
| 2026-10-18 | **Ubuntu homelab** (Ryzen 5 5600, ~14 GiB; Ubuntu 26.04, kernel 7.0.0-38; host PG 18.6; NVMe ext4) | `8c581ad` | mvp (config counts, `chunk_rows: 5000`, 1 worker) | 10,000 / 18,776 / 12,265 | 6 | 1.78 s | 1.04 + 0.02 s | 66 MB | 16.5 MB | 19.7 MB | 12/12, 0.21 s |
| 2026-10-17 | macOS dev (M1, 8 GB; Docker PG 17.11, VM 8 CPU / 4 GB) | `1e98a36` | mvp (config counts, `chunk_rows: 5000`, 1 worker) | 10,000 / 18,776 / 12,265 | 6 | 2.04 s (load 1.9 s) | 1.17 + 0.03 s | 63 MB | 16 MB | 19 MB | 12/12, 0.40 s |

Measured with `/usr/bin/time -l` (macOS) around `ecomgen generate`; WAL = `pg_wal_lsn_diff` across the run (server-wide;
no other activity on the server at the time). Largest tables: order_items 3.9 MB, orders 3.2 MB, payment_attempts 2.7 MB.

## Ubuntu Phase 3 release verification (`v0.3.0-homelab`, 2026-10-19)

`v0.3.0-homelab` → `0301e1f` (PRs #21-#24; CI 378 tests / 0 skipped on PG 17.11 and 18.6). Run by the user on the
homelab one step at a time (host PG 18.6, 127.0.0.1:5432 only; NVMe mounted rw):

| Check | Result |
|---|---|
| Pre-checks (read-only) | mount, ports (5432 / 55432 localhost only), DB inventory, 4 VLSIM containers healthy, env files 600 |
| Upgrade | `git checkout v0.3.0-homelab` + `uv sync --frozen` (dependencies unchanged since v0.2.0); `v0.2.0-homelab` kept for rollback |
| Migration 0008 | applied first in a new disposable DB `ecomgen_v030_smoke` (all 0001-0008); existing DBs not migrated yet |
| Smoke `generate` + `validate` | 20/20; files byte-identical to `scripts/expected_smoke_feed_digest.txt` (data version 3, cross-machine) |
| Clock-step chain (10k config, own schema set) | 9 steps: day 1, six daily steps (0.3-0.5 s each), Jan 8 → 15, Jan 15 → end (3.1 s); repeat of a done step = no-op |
| Recovery | SIGKILL after a step's DB commit, before its files → RUNNING, `advance` refused, `resume` published the files; validate 20/20; second resume no-op |
| Stepped = one-shot | all 7 tables identical (md5 per table) to the one-shot 10k run in `ecommerce_oltp`; settlement records identical; 110,000 clickstream records |
| Host snapshot diff | changes only in the git clone (checkout, fetched refs, `__pycache__`); no VLSIM or `postgresql@18-main` change. The "before" file lacked the database section (taken without `PG_LOCAL_SUPERUSER=1`); the after-values show `ecommerce_oltp` and the benchmark DBs unwritten (update counters 1-3 = their own runs) |

Finding: a browsing event at exactly `2026-01-08T00:00:00Z` belongs to the step ending then but lands in
`date=2026-01-08`, so day-aligned steps can still split a day for `replay-files` (see the user guide).

## Ubuntu Phase 2 release verification (`v0.2.0-homelab`, 2026-10-18)

Operator-run per `docs/HOMELAB_DEPLOYMENT.md` "Phase 2 release": tag `v0.2.0-homelab` = `main` `de90a6a` (PR #18
merge, tree = reviewed head `959fce9`, CI 251 tests / 0 skipped on PG 17.11 and 18.6), clone
`/srv/homelab/projects/ecommerce-homelab` at the tag, clean tree. The three databases held the `v0.1.1-homelab` run;
after a read-only inventory (owner `ecomgen_app`; 9.6 / 19 / 19 MB) the operator dropped and recreated them by hand
(approved), empty, owner `ecomgen_app`, `CONNECT`/`TEMPORARY` revoked from `PUBLIC`. Port 5432 (`SHOW port`), data
root `/srv/homelab/ecommerce/data` on the NVMe (UUID `d263e3fd-…`, rw, hard links work). Output
`/srv/homelab/ecommerce/benchmarks/20261018T073745Z`. `scripts/homelab_benchmark.sh`: **49/49 PASS, 0 FAIL, exit 0,
all expectations passed**; replay cleanup as a dry run (`CLEANUP_REPLAY` unset).

| Phase | Result |
|---|---|
| Preflight | PG 18.6 on 5432; backend PID in the `postgresql@18-main` cgroup; role with no privileges/memberships owning only the three empty databases; both feeds enabled |
| Smoke 1k (DB + files) | 1.04 s, 50 MB RSS, server 133 MB; validate (DB + files) pass; **files identical to the macOS reference**; no orphans or staging |
| Publish crash 1k (`*_pc` schemas in the smoke DB) | SIGKILL after placing file 4, before its ledger row → exit 137; no sessions/prepared xacts left; validate fails as designed; resume 0.79 s adopts the placed file; validate pass; **files identical to the uninterrupted reference, rows identical to the smoke run**; ledger == disk |
| MVP 10k (DB + files) | 4.62 s wall (3.44 + 0.11 s CPU), 68 MB RSS; server 179 MB peak, CPU 0.73 s; WAL 16.6 MB; DB 20.0 MB; **2,165 orders/s, 10,429 rows/s** end-to-end; validate 1.35 s; **rows and files identical to the macOS references** |
| Files (10k) | payment_settlement 88 files / 8,295 rows / 1.0 MB; clickstream 31 files / 110,000 events / 30.6 MB; manifest 1 file / 119 entries; sha256 re-verification of 120 files (31.6 MB) at 1,072 MB/s, 0 mismatches, 0 orphans, 0 staging |
| Feed-only throughput | payment_settlement 10,139 rows/s (1.2 MB/s, 63 MB RSS); clickstream 48,156 events/s (12.8 MB/s, 62 MB RSS) |
| Load crash + resume | SIGKILL before orders chunk 1 → exit 137; no leftovers; status reports the crash; validate fails and generate refuses as designed; resume 3.71 s (68 MB RSS, server 283 MB); second resume no-op; **rows and files identical to the clean run**; no orphans or staging |
| Replay (2026-01-05..10, delay 24 h, jitter 6 h, 10 % dup) | SIGKILL after delivery 6 → exit 137; rerun finishes (`rpl_aba99c06f638`: 27 deliveries, 3 duplicates, 21 new, 0.30 s); second rerun only verifies; **replayed originals identical to the macOS reference**; deliveries = the plan exactly |
| Cleanup (dry run) | replay proven created by this run; would delete 27 items (6.1 MB), only this replay's copies (`cleanup_plan.txt`); nothing deleted |
| Totals | smoke 11.9 MB (both schema sets), test 20.1 MB, crash 20.0 MB; data root 73.8 MB; `/srv/homelab` 598 MB → 703 MB used, 906 GB free |

Untouched (host snapshot as root, read-only, `before-v0.2.0.txt` after the recreate vs `after-v0.2.0.txt`, 0
unreadable paths, Docker readable): the **only** differences are the three `db` lines of `ecommerce_homelab_{crash,
smoke,test}` (size 7.9 MB → 19.9 / 12.5 / 20.0 MB; inserted 48,315 / 10,054 / 48,344 rows; 2-3 updates; 0 deletes).
Identical: role `ecomgen_app` and the ACLs, other roles and memberships, server settings, the `postgres` database,
the `postgresql@18-main` start time, Docker containers (VLSIM engine + TMS, PostgreSQL 17 on 55432, Redpanda),
volumes, ports, and every file under `/srv/homelab` outside the project path and clone.

Compared with the macOS PG 17 rehearsal: MVP end-to-end 4.62 vs 4.74 s; feed-only 10,139 vs 9,157 rows/s (CSV) and
48,156 vs 44,520 events/s (JSONL); same data, files and WAL. The feeds account for most of the 10k run (feed-only
0.95 + 2.41 s, mostly CPU in clickstream rendering); database-only was 1.78 s on `v0.1.1-homelab`.

## Ubuntu homelab verification (2026-10-18)

Deployment gate, operator-run per `docs/HOMELAB_DEPLOYMENT.md` at `main` `8c581ad` (= reviewed PR #12 tree), clone
`/srv/homelab/projects/ecommerce-homelab`, output `/srv/homelab/ecommerce/benchmarks/20261018T010412Z`. Host
PostgreSQL 18.6 (`18/main`), role `ecomgen_app` (no privileges, no memberships) owning only
`ecommerce_homelab_{smoke,test,crash}`, created by hand; nothing dropped. `scripts/homelab_benchmark.sh`: **all
expectations passed**.

| Phase | Result |
|---|---|
| Preflight | NVMe `/srv/homelab` UUID `d263e3fd-041a-4070-a72a-28e01126aa6f` read-write; approved commit, clean tree; role/databases checked; backend PID in the `postgresql@18-main` cgroup |
| Smoke 1k | init-db, generate, validate pass; 0.30 s, 43 MB RSS; server 134 MB (2 samples only: too short to sample well) |
| MVP 10k | 1.78 s wall, **5,618 orders/s, 27,068 rows/s** (48,181 rows); generator 66 MB RSS; server unit 172 MB peak (incl. page cache), CPU 0.70 s; WAL 16.5 MB (server-wide, cluster otherwise idle); DB 19.7 MB; validate 12/12 in 0.21 s; **data identical to the macOS reference** (7-table digest) |
| Crash + resume | SIGKILL before orders chunk 1 → exit 137; no client sessions or prepared transactions left; status reports the crash; validate fails and generate refuses as designed; resume 0.87 s (54 MB RSS, server 208 MB, CPU 0.33 s); validate 12/12; second resume no-op; **resumed data identical to the clean run** (no duplicates, nothing missing) |
| Totals | smoke 9.4 MB, test 19.7 MB, crash 19.7 MB; `/srv/homelab` 906 GB free before and after |

Untouched (host snapshot as root, read-only, before Stage 2 vs after the benchmark, ~2,850 lines): the only
differences are role `ecomgen_app` (`super=false createdb=false`), the three databases' ACLs (owner and sole
`CONNECT` = `ecomgen_app`) and their sizes / write counters. Identical: Docker containers (IDs, start times, restart
counts: VLSIM engine + TMS, PostgreSQL 17 on 55432, Redpanda), volumes, networks, images, listening ports, files under
`/srv/homelab` outside the project path and clone, other roles and memberships, all non-default server settings,
the `postgres` database, the `postgresql@18-main` unit's start time.

Compared with macOS (10k): same data and WAL; Ubuntu 11% faster wall (1.78 vs 2.00 s) with less server CPU
(0.70 vs 1.04 s). Generated CSV/JSONL files: not in scope yet (Phase 2 file feeds, `TASKS.md`); `data/` is empty.

## Restart (Phase 1 gate: "restart works")

2026-10-17, same environment and config, separate fresh database:

1. `generate` SIGKILLed just before writing orders chunk 1 → exit 137; run left `RUNNING`, 5,000/10,000 orders committed.
2. `status`: "no process is writing: the run crashed"; `validate`: `run_completed` + `ledger_complete` fail (as expected).
3. `generate` refuses (run exists, points to `resume`).
4. `resume`: 1 chunk written, 5 skipped, 1.0 s; `validate` 12/12; a second `resume` is a no-op.
5. All 7 source tables identical to the clean run (row count + md5 of rows).

## Reference data (cross-platform determinism)

`scripts/expected_mvp_digest.txt` holds row counts + md5 per table for the 10k run above (seed 20261017,
`DATA_VERSION` 1; rows rendered in UTC, sorted bytewise). `scripts/homelab_benchmark.sh` requires any machine's 10k run to match it exactly.

Dry run of `scripts/homelab_benchmark.sh` on macOS after its review fixes (2026-10-17, dev Docker server; the kept
`ecommerce_homelab_test` was dropped afterwards): all expectations passed; smoke 0.69 s / 40 MB; 10k 3.57 s / 60 MB,
PG container 218 MB peak (sampled), PG CPU 2.64 s, WAL 16.4 MB, DB 19.5 MB; identical to the reference; resume
1.12 s. Host snapshot diff: only the new benchmark database; dev `ecommerce_oltp` size and counters identical.
(Wall times vary run to run on the laptop: compare 2.0-3.6 s across the three macOS 10k runs.)

Host-PostgreSQL tooling rehearsals (2026-10-18, `fix/host-postgres-tooling`, least-privilege `ecomgen_app` owning
three hand-created databases, nothing dropped by the script; all expectations passed both times):

| Server | 10k wall | Throughput | Generator RSS | Server peak | WAL | DB size | Resume |
|---|---|---|---|---|---|---|---|
| Docker PG 17.11 (test DB `LOCALE 'C'`, `timezone America/New_York`) | 2.00 s | 5,000 orders/s, 24,090 rows/s | 63 MB | 232 MB | 16.4 MB | 19.4 MB | 1.15 s |
| Docker `postgres:18` (18.6) | 2.25 s | 4,444 orders/s, 21,414 rows/s | 63 MB | 184 MB | 16.5 MB | 19.6 MB | 0.95 s |

Both matched the reference digest, which is now pinned to UTC + `COLLATE "C"` (the earlier digest depended on the
server's time zone and collation: it changed under `America/New_York` and `C`). The reference was recomputed from the
same 10k database, which still matched the old reference under the old method. PG 18 snapshot diff (superuser,
read-only): only `ecomgen_app`, its three databases' ACLs and their sizes/counters.

## File feeds (Phase 2, macOS dev, 2026-10-18, `feat/file-feeds`)

10k config (`clickstream_events: 100000`), throwaway database, data root on the laptop SSD: `generate` 5.72 s wall
(3.84 + 0.27 s CPU; database part ~2 s as before), peak RSS 66 MB; 120 files, 31 MB: 8,295 settlement rows and
110,000 clickstream events (100,000 browsing + 10,000 `order_placed`); `validate` 19/19 in 1.78 s; repeat `resume` a
no-op (0.15 s). A bounded entity cache took feed writing from 8.8 s to 2.6 s with byte-identical output (golden test);
per-directory fsyncs added ~0.8 s.

## Phase 2 benchmark rehearsals (macOS, 2026-10-18, `chore/benchmark-files`)

`scripts/homelab_benchmark.sh` with files, least-privilege role, hand-created databases, laptop SSD (timings vary
with laptop load; the Ubuntu run is the reference):

| Server | Checks | MVP end-to-end (DB + files) | Feed-only CSV | Feed-only JSONL | sha256 verify | Cleanup |
|---|---|---|---|---|---|---|
| Docker PG 17.11 | 53/53 (after review fixes; first run 48/48) | 4.74 s, 65 MB RSS, server 358 MB | 8,295 rows in 0.91 s (9,157/s, 1.1 MB/s) | 110,000 events in 2.47 s (44,520/s, 11.8 MB/s) | 120 files, 31.6 MB, 1,460 MB/s (cached) | `CLEANUP_REPLAY=1`: all copies of the run's replay (25-26), audit written |
| `postgres:18.6-trixie` | 49/49 (first run 44/44) | 1,852 orders/s end-to-end | 8,575/s | 46,621/s | 1,406 MB/s (cached) | dry run only |

Both: smoke and publish-crash files identical to the uninterrupted reference; MVP rows and files identical to the
references (`scripts/expected_{mvp,feed}_digest.txt`, recorded on PG 17); replay killed after delivery 6 → rerun =
exactly the plan; replayed originals = `scripts/expected_replay_originals.txt`; no orphans or staging left.
WAL 16.7 MB (10k). Data root after a full run ≈ 70 MB.

## Half-open step windows (macOS, 2026-10-19, `fix/half-open-windows`, data version 4)

Windows `[previous, as_of)` instead of `(previous, as_of]`. In a throwaway `postgres:18.6-trixie` container:

- `scripts/homelab_benchmark.sh` against the **unchanged** references: 49/49, exit 0. The 10k one-shot rows, smoke
  and MVP files and replay originals are byte-identical to data version 3 (no record of the example config falls
  exactly on `clock_end`).
- 10k e2e: 31 daily `advance` steps vs one `generate`: identical table digests (orders, items, payments,
  customers), identical file records (hash `cd41dcb4…`, the same as under data version 3), validate 20/20 for both.
  No date partition holds files of two steps; the example config has one browsing event at an exact midnight, which
  data version 3 would have split.

## Browsing sessions per clock step (macOS, 2026-10-19, `fix/session-ordering`, data version 3)

Browsing events of 31 daily steps over the example clock (January 2026, chunk_rows 5000), pure generation without
the database (`write_staged`'s browsing part), before (data version 2: every step generates every session and keeps
its window) and after (sessions in time slots; a step generates only those started in
`(previous - 25 min, as_of]`; `[previous - 25 min, as_of)` from data version 4):

| Browsing events | Daily step before | Daily step after | 31 steps before | 31 steps after | validate's browse count (day 20) |
|---|---|---|---|---|---|
| 100,000 | 1.24 s (median) | 0.083 s | 40.2 s | 2.6 s | 0.97 s → 0.002 s |
| 1,000,000 | 11.6 s | 0.46 s | 366 s | 14.4 s | 8.8 s → 0.017 s |

After the change the cost of a step follows the sessions in its window, not the history: 31 steps together cost
about what generating every session once costs. End-to-end on Docker `postgres:18.6-trixie`, 10k config: 31 daily
`advance` steps took 36 s (~1.2 s each, mostly CLI start and database writes); rows (table digests) and file records
(110,000 clickstream events, all settlements) identical to one `generate`; validate 20/20 for both.
`scripts/homelab_benchmark.sh` with `RECORD_REFERENCES=1` re-recorded the file references
(`scripts/expected_{smoke_feed,feed}_digest.txt`, `expected_replay_originals.txt`); `expected_mvp_digest.txt` is
unchanged (rows did not change). A following verification run: 49/49, exit 0.

Remaining: the first step of a chain still generates every session up to its `as_of` (it publishes all of them);
slots make browse volume nearly flat (each day of the example gets 645-646 sessions, the last day 634 as no session starts in the last 25 min; before, uniform random
starts varied by a few percent day to day), like order volume; time-of-day or weekday shapes are not modelled.

## Not yet measured

- Larger profiles (practice 100k, advanced 1M) and `workers: 2`.
