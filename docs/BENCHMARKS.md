# Benchmarks

Measured runs, not promises (charter: record time, peak RSS, WAL growth, footprint and CPU per completed run).
Add a row per run; note the environment. The target is the Ubuntu server; macOS rows are development baselines.

## Runs

| Date | Env | Commit | Profile | Orders / items / payments | Chunks | Wall | CPU user+sys | Peak RSS | WAL | DB size | validate |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-18 | **Ubuntu homelab** (Ryzen 5 5600, ~14 GiB; Ubuntu 26.04, kernel 7.0.0-38; host PG 18.6; NVMe ext4) | `8c581ad` | mvp (config counts, `chunk_rows: 5000`, 1 worker) | 10,000 / 18,776 / 12,265 | 6 | 1.78 s | 1.04 + 0.02 s | 66 MB | 16.5 MB | 19.7 MB | 12/12, 0.21 s |
| 2026-10-17 | macOS dev (M1, 8 GB; Docker PG 17.11, VM 8 CPU / 4 GB) | `1e98a36` | mvp (config counts, `chunk_rows: 5000`, 1 worker) | 10,000 / 18,776 / 12,265 | 6 | 2.04 s (load 1.9 s) | 1.17 + 0.03 s | 63 MB | 16 MB | 19 MB | 12/12, 0.40 s |

Measured with `/usr/bin/time -l` (macOS) around `ecomgen generate`; WAL = `pg_wal_lsn_diff` across the run (server-wide;
no other activity on the server at the time). Largest tables: order_items 3.9 MB, orders 3.2 MB, payment_attempts 2.7 MB.

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

## Not yet measured

- Larger profiles (practice 100k, advanced 1M) and `workers: 2`.
