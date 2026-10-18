# File feeds (Phase 2)

External source files written by `generate` / `resume` next to the PostgreSQL rows. Code: `src/ecomgen/feeds.py`.
Feeds are derived from the same `World` as the database rows, so business IDs agree by construction; `validate`
proves it from the files themselves. Clean data only: anomalies (late, duplicate, malformed rows) are Phase 4.

## Where

Data root: `$ECOMGEN_DATA_ROOT` if set (per machine, like the DB URL), else `filesystem.root`. It must already exist;
the generator never creates it. Homelab: `/srv/homelab/ecommerce/data`.

```text
<root>/
  source_feeds/payment_settlement/date=YYYY-MM-DD/<run_id>-<provider>-part-NNNNN.csv
  clickstream/date=YYYY-MM-DD/<run_id>-part-NNNNN.jsonl
  manifests/run_id=<run_id>/manifest.json
  staging/<run_id>/            # incomplete output of a run in progress; never read by consumers
```

Partition = UTC date of the event time (`settled_at`, `event_time`). Files roll over to the next part at
`filesystem.max_file_size_mb`. Rows in a file follow generation order, not event time. Toggles:
`filesystem.csv` (settlements), `filesystem.jsonl` (clickstream); `parquet` is not implemented and is refused.

## Contracts (schema_version 1)

**payment_settlement** (`source_system` payment_processor): one row per payment attempt in state `SETTLED` at
`clock_end`, in daily batches per provider. Header row in every file.

| Column | Meaning |
|---|---|
| settlement_id | `stl_N` for payment `pay_N` |
| batch_id | `<provider>-<settlement_date>` |
| payment_id, order_id | PostgreSQL IDs |
| provider, method, currency | as on the payment attempt |
| gross_amount | payment amount (= order total) |
| fee_amount | `round_half_up(gross × pct / 100 + fixed, 0.01)`, capped at gross; pct/fixed per provider (`FEES`) |
| net_amount | gross − fee (exact decimal) |
| settled_at | the payment's SETTLED time, `YYYY-MM-DDTHH:MM:SSZ` |
| settlement_date | UTC date of settled_at (the partition) |

**clickstream** (`source_system` web_app): JSON object per line with `event_id, schema_version, event_type,
customer_id, session_id, product_id, event_time, arrived_at, source_system, payload`.

- Browsing: `counts.clickstream_events` events `evt_N` in sessions `ses_K` of 5 (the last may be shorter); one
  customer per session or anonymous (`customer_id` null, ~30%). Types: `product_viewed` (payload `page`),
  `search` (no product; `query`, `results`), `added_to_cart` / `removed_from_cart` (`quantity`), `wishlist_added`.
  Products are active ones; customers are ACTIVE ones.
- Orders: one `order_placed` event `evt_ord_N` per order at `placed_at`, session `ses_ord_N`, payload `order_id`,
  `currency`, `total`.
- `event_time` is in `[clock_start, clock_end)`; `arrived_at` = event_time + 1-30 s.

**manifest.json**: `run_id`, `generator_version`, `data_version`, `config_sha256`, `seed` and per file
`source_system, feed, file_path` (relative to the root)`, format, schema_version, start_event_time, end_event_time,
records_written, bytes, sha256, anomaly_count` (0) `, published_at`. All timestamps are `YYYY-MM-DDTHH:MM:SSZ`;
`published_at` is when the ledger recorded the file (for a file adopted after a crash: the resume time). Built only
from the database, so rebuilding it gives the same bytes.

## Publishing and recovery

DB and filesystem writes can't commit together; the run converges instead:

1. All database chunks commit first (chunk ledger, unchanged).
2. Every file is regenerated into `staging/<run_id>/` (cleared first: only that run's unpublished output).
3. Per file: fsync, create missing partition directories (fsyncing each parent), hard-link into place (same
   filesystem; unlike rename, a link fails if a file appeared at the target meanwhile), fsync the directory, then a
   `published_files` ledger row (migration 0005). The manifest is published the same way, last. Then the run
   becomes `COMPLETED`.
4. A published file is **never overwritten**. On `resume` (or any repeat): a recorded file must still match its
   checksum; an unrecorded file at a target path (crash between placing and recording it) is adopted, after an
   fsync, only if its bytes equal the regenerated ones; anything else stops with an error for a human to
   reconcile. Once the manifest is recorded, a repeat regenerates nothing: it only verifies the recorded files and
   that the manifest lists exactly them.

Consumers: a file is complete once it appears outside `staging/`; a run's set is complete once its manifest
exists. Resume refuses if the data root or feed layout differs from the run's (`resume_fields`; the root is
recorded only when a feed is enabled). The data root's filesystem must support hard links (ext4, APFS do).

## Validation (`ecomgen validate`)

For runs that publish feeds, in the same read-only snapshot as the database checks: `files_match_ledger` (exists,
readable, size, sha256, record count) and `manifest_matches_ledger`; for a published settlement feed
`settlements_match_payments` and `settled_payments_settled_once`; for a published clickstream
`clickstream_ids_exist`, `orders_placed_once` and `events_unique_and_complete`. A missing field in a row is a
violation (NULL-safe comparisons). Feed rows are streamed into temporary tables (created just before the snapshot,
dropped after), so memory stays bounded and the role needs `TEMPORARY` on the database (owners have it).

## Not yet

`replay-files --from --to`, retention/cleanup policy, Parquet snapshots, supplier/courier feeds (need warehouses,
inventory, shipments), anomaly injection (Phase 4).
