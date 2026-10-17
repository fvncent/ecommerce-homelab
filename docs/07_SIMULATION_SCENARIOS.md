# 07 — Simulation Scenarios

The generator should model one logical business timeline, then separately model the reliability and timing of independent sources.

| Scenario | Business behavior | Source / pipeline challenge | Pass criterion |
|---|---|---|---|
| Normal weekday | Typical orders, payments, fulfillment | Routine incremental ingest | Numbers reconcile |
| Holiday sale | High traffic, popular-product skew | Throughput, hot keys, late clickstream | No lost orders |
| Payment outage | More failed attempts and retries | CDC state changes, delayed settlement CSV | Payment states reconcile |
| Supplier delay | Inventory updates arrive late | Watermarks / out-of-order files | Latest valid inventory restored |
| Fulfillment split | Multiple warehouses ship one order | Multi-table joins, partial shipments | Correct order status |
| Return surge | Return/refund events days after sale | Backfills and incremental correction | Net revenue recalculated |
| Corrupted supplier feed | Missing product ID, malformed row | Quarantine, contracts, alerting | Errors isolated, source intact |
| Schema release | Adds or changes optional fields | Schema evolution + compatibility | Version-aware processing |
| Generator crash | Interrupted batch or DB/file gap | Recovery and idempotence | No duplicate business entities |
| CDC consumer outage | Stop downstream CDC for a period | WAL backlog and catch-up | Monitored lag; recover safely |

## Scenario model fields

- `scenario_id`, `scenario_version`, `seed`, `start_time`, `end_time`, `time_multiplier`
- customer arrival distribution, region mix, product popularity distribution
- payment authorization and settlement delay distributions
- order-to-shipment / delivery lead-time distributions
- source-file production cadence and late-file distributions
- injection switches / rates, eligible record types, exception limits
- expected outputs and metrics to score separately

## Important rule

An event arriving late is **not necessarily bad**. Record its valid business time and its delayed arrival time distinctly. Similarly, duplicate delivery does not mean duplicate business orders. Ground truth should identify whether a record is invalid, late but valid, duplicated delivery, or a legitimate revision.
