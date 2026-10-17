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

## Not yet measured

- Ubuntu server (Ryzen 5 5600, 16 GB) — the real target; Phase 0 inventory pending.
- Larger profiles (practice 100k, advanced 1M) and `workers: 2`.
