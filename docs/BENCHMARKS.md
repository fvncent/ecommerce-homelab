# Benchmarks

Measured runs, not promises (charter: record time, peak RSS, WAL growth, footprint and CPU per completed run).
Add a row per run; note the environment. The target is the Ubuntu server; macOS rows are development baselines.

## Runs

| Date | Env | Commit | Profile | Orders / items / payments | Chunks | Wall | CPU user+sys | Peak RSS | WAL | DB size | validate |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-17 | macOS dev (M1, 8 GB; Docker PG 17.11, VM 8 CPU / 4 GB) | `1e98a36` | mvp (config counts, `chunk_rows: 5000`, 1 worker) | 10,000 / 18,776 / 12,265 | 6 | 2.04 s (load 1.9 s) | 1.17 + 0.03 s | 63 MB | 16 MB | 19 MB | 12/12, 0.40 s |

Measured with `/usr/bin/time -l` (macOS) around `ecomgen generate`; WAL = `pg_wal_lsn_diff` across the run (server-wide;
no other activity on the server at the time). Largest tables: order_items 3.9 MB, orders 3.2 MB, payment_attempts 2.7 MB.

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

## Not yet measured

- Ubuntu server (Ryzen 5 5600, 16 GB, host PostgreSQL 18) — the real target.
- Larger profiles (practice 100k, advanced 1M) and `workers: 2`.
