# 05 — Testing, Validation and Acceptance Plan

## MVP gates

| ID | Scenario | Expected result |
|---|---|---|
| T01 | Fresh installation using Compose or existing PG | Migrations succeed; database remains persistent |
| T02 | Same seed, config and version twice in clean environments | Identical logical entities, counts and relationships |
| T03 | Generate 10k orders with line items, payments and stock movement | All FK/state/financial invariants pass |
| T04 | Run a second increment day | Existing IDs untouched; new entities/changes appear correctly |
| T05 | Crash halfway through writing a batch file | Staging file is not visible as complete; resume republishes safely |
| T06 | Crash after DB write but before file publish | Reconciliation finishes without duplicate orders/paid rows |
| T07 | Duplicate event delivery | Generator ledger flags duplicate; event IDs stable |
| T08 | Inject a malformed CSV row | The manifest records exactly which record is intentionally bad |
| T09 | Generate out-of-order and late clickstream rows | Event_time and arrived_at distinct and documented |
| T10 | Re-run same completed chunk | No duplicate rows/files/manifests are produced |
| T11 | Config with invalid output directory/DB URL | Fail early with actionable diagnostics and no misleading success |
| T12 | Benchmark medium profile on 16 GB server | Peak memory, rows/sec, disk and WAL growth captured, not guessed |

## Data invariants to enforce

- Every `orders.customer_id` resolves to exactly one customer.
- Every order item belongs to an order and references a valid product.
- Monetary order totals match documented line/discount/tax/shipping rules using decimal arithmetic.
- Captured amount and settlement/refund totals reconcile under explicitly documented timing/business assumptions; pending payments need not equal settled amounts at every instant.
- Shipping and return timestamps obey accepted business transition rules.
- Inventory can be reconstructed from movement events where movement tracking is enabled.
- Each outbox event is associated with one committed business transaction and has a stable unique ID.
- Every published file matches its manifest checksum and count.

## Fault injection benchmark matrix

Test under: no anomalies, 1% duplicate event delivery, 3% late events, selected bad CSV/JSONL rows, source downtime, generator process kill, SQL connection failure, disk-nearly-full prevention and schema v1→v2 change.

Specify anomaly rates per **eligible records**, not as independent random guarantees. Actual observed rates and record IDs belong in the private manifest.

## Quantitative metrics

- `canonical_fk_violations`: target 0
- `canonical_invalid_state_transitions`: target 0
- `canonical_financial_reconciliation_errors`: target 0 (given declared tolerances and pending-state policy)
- `published_files_missing_manifest`: target 0
- `manifest_checksum_mismatches`: target 0
- `restart_duplicate_logical_records`: target 0
- `generated_rows_per_second`: measured, not assumed
- `peak_generator_rss_mb`: measured
- `postgres_wal_bytes_generated`: measured where feasible
- `storage_bytes_written`: measured
- `injected_anomalies_expected` vs `detected`: computed per case

## Proving reliability

Run at least one stop/restart during a real generation and compare the final results with a clean run of the same logical timeline. Verify **business-equivalent** output across retries; exact byte equality of Parquet files may require additional serialization controls.
