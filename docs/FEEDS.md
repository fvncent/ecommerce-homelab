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

## Replay (`ecomgen replay-files`)

Simulated historical delivery of a COMPLETED run's files (code: `src/ecomgen/replay.py`):

```bash
ecomgen replay-files CONFIG --from 2026-01-05 --to 2026-01-08 [--run-id R] [--feed clickstream]... \
    [--delay-hours 24] [--jitter-hours 0] [--duplicate-pct 0]
```

- **Originals come from the run manifest.** The manifest must match its ledger checksum, and every original whose
  partition date is in `[from, to]` must match the manifest (size + sha256). Otherwise nothing is written and the
  problems are listed.
- **Deliveries are byte-identical copies** at `deliveries/replay_id=<id>/arrived=<D>/<original path>`; a duplicate
  delivery is `<name>.dup1.<ext>`. Originals under `source_feeds/` and `clickstream/` are never touched.
- **Arrival:** end of the partition's day + `delay_hours` + up to `jitter_hours` (whole seconds); a duplicate
  (`duplicate_pct` of files) arrives 1-24 h after its first delivery. Deterministic: from a hash of the replay id
  and the original path. Real-time pacing (waiting until arrival) needs the Phase 3 clock.
- **Replay id** `rpl_<12 hex>` = hash of run id + parameters + `REPLAY_VERSION`. The same command is the same
  replay: repeating it finishes an interrupted one or verifies a finished one; other parameters make a new replay.
- **delivery_manifest.json** (written last): `replay_id, run_id, kind: replayed_delivery, params,
  source_manifest {file_path, sha256}` and per delivery `delivered_path, original_path, feed, partition_date,
  sha256, bytes, arrived_at, duplicate`. Built only from the plan, so it is reproducible byte for byte.
- **Crash-safe like publishing:** copy into `staging/<replay_id>/`, check the copy's sha256, place with the same
  never-overwriting `place_file`, then a `file_deliveries` row (migration 0006); the manifest's row and
  `replays.state = COMPLETED` commit together. Holds the run lock (never overlaps generate/resume/cleanup).
  Tested with real SIGKILLs before placing a file, after placing it but before its row commits, and after placing
  the manifest but before COMPLETED: the rerun adopts the placed file, and the ledger equals what is on disk.
- **Path safety:** manifest paths must be plain relative paths under their feed's directory (no `..`, `.`, empty
  components, backslashes or absolute paths), and no symlink is followed below the data root (originals, staging,
  delivery targets; publishing too). The schema repeats the path rules and ties every delivery to its replay's
  directory and to a file the run published.
- Refused (exit 1): a run that isn't COMPLETED, published no files, published under another data root or lacks a
  requested feed; no files in the range; unsafe paths, symlinks, missing or changed originals. Bad arguments
  (dates, hours outside 0..87600, percent outside 0..100, no or unknown feeds) exit 2; no run / not migrated
  exit 3. A verify of a finished replay checks only the deliveries (originals may have been cleaned up since).
  The CLI's "newly delivered" counts files, not the delivery manifest.

## Cleanup (`ecomgen cleanup-files`)

Explicit, reviewed deletion of files the generator produced (code: `src/ecomgen/cleanup.py`):

```bash
ecomgen cleanup-files CONFIG --replay-id rpl_...          # dry run: candidates, kept files, a token
ecomgen cleanup-files CONFIG --replay-id rpl_... --delete --confirm <token>
# targets: --replay-id (a COMPLETED replay's copies) | --run-id (a COMPLETED run's feed files) | --staging
```

- **Candidates come only from the database**: the target's recorded, not-yet-deleted files (`published_files` /
  `file_deliveries`), or `staging/<id>` directories of runs, replays or cleanups this database knows. Nothing is found by
  scanning: unrecorded files (and unknown staging entries) are never deleted. Run manifests, delivery manifests and
  audit reports are never candidates (the schema also refuses marking a manifest deleted).
- **Dry run by default.** It inspects every candidate (hashing it); the token is a hash of the target and the exact
  list (path, checksum, size, already gone). `--delete --confirm TOKEN` rebuilds the list and refuses, with no side
  effects, unless it matches, so only what was reviewed is deleted. A file whose bytes no longer match its record
  is not a candidate ("changed since it was recorded: kept"), so it doesn't come back as work every time.
- **Never follows a symlink:** each file's directory is opened component by component from the data root with
  `O_NOFOLLOW`, and the file is checked (regular file, recorded checksum, same inode) and unlinked through that
  descriptor; staging directories are removed with fd-based `rmtree`. Swapping a directory for a link at any moment
  can't redirect a deletion outside the root (tested). A file changed during the delete is kept (`kept_changed`).
- **Root guards:** refuses a missing root, `/`, your home directory, a root containing `.git` or `PG_VERSION`, and
  a run/replay published under another root. Holds the run lock (never overlaps generate/resume/replay).
- **Ledger rows are never removed**: marked `deleted_at` / `deleted_by` (migration 0007). `validate` reports a
  cleaned run's files as "deleted by cleanup <id>" (and fails); replay refuses cleaned originals, and a cleaned
  replay can't be verified (both say so). Copies made by earlier replays are independent of their originals.
- **Audit:** `audit/cleanup_id=cln_<UTC>_<random>.json`: target, token, data root, deleted files (path, sha256,
  bytes, ledger, deleted_at), `deleted_bytes`, files already gone (marked, not counted as deleted), staging dirs
  removed, files kept because they changed. Built only from the database and placed without overwriting.
- **Crash-safe:** cleanups row (RUNNING, with its data root) first; per item delete → fsync dir → one transaction
  marking its ledger row and recording it in the cleanup's details; then audit, COMPLETED. After an interruption,
  the next accepted `--delete` under the same root first finishes earlier cleanups' audits; a new dry run lists
  what is left, and files unlinked but not yet marked show as "already gone" (tested with a real SIGKILL).
- Works with databases before migration 0007 for everything else (validate, replay treat nothing as cleaned);
  cleanup itself then exits 3 asking for `init-db`.
- Exit codes: 0 dry run or deleted, 1 refused/failed, 2 config/argument error (including an empty id), 3
  run/replay not found or not migrated. No age-based selection (`--older-than`): left out on purpose (2026-10-18).

## Benchmark

`scripts/homelab_benchmark.sh` exercises all of the above end to end (publish crash, load crash, replay crash,
cleanup) and compares the files with macOS references: see `docs/HOMELAB_DEPLOYMENT.md`.

## Clock steps (`ecomgen advance`)

A step publishes the events of its window `(previous_as_of, as_of]` (first step `[clock_start, as_of]`, UTC):
settlements by `settled_at`, `order_placed` by `placed_at` (the orders the step inserts), browsing events by
`event_time`. Same staging, never-overwrite placement, ledger and manifest-last protocol as `generate`, after the
step's database chunks and before COMPLETED, so `resume` finishes any interrupted part. The step manifest adds
`clock_step` (`first_step_run_id`, `previous_as_of`, `as_of`, `window`, `window_start`); a one-shot manifest is
unchanged. All steps' records together = the one-shot run's records. `validate` checks every step's files together.
`arrived_at` may be up to 30 s after the step's `as_of` (receive time, not the window). Cost: each step reads all
browsing sessions to pick its window (~2.5 s per step at 100k events on the Mac).

## Not yet

Age-based retention, Parquet snapshots, supplier/courier feeds (need warehouses,
inventory, shipments), anomaly injection (Phase 4).
