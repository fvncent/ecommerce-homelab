# Practice dataset runbook (for building your pipeline)

A step-by-step setup of a **practice dataset**: a source system your ETL / ELT pipeline can read from. You get an
e-commerce OLTP database that changes day by day, a transactional outbox (change log), warehouse stock with its
movement log, and daily CSV / JSONL files.
Everything runs on the Ubuntu homelab; you type the commands yourself. Each step says whether it only **reads** or
**changes** something.

For the full reference (every command, all queries) see `docs/GENERATOR_USER_GUIDE.md`; for file formats
`docs/FEEDS.md`. Rehearsed end to end on the Mac (2026-10-19, `feat/outbox`) in a throwaway PostgreSQL 18.6 with the
same layout (an older dataset in `commerce` stayed byte-identical); the first Ubuntu run is yours. Re-rehearsed for
inventory (2026-10-21, `feat/inventory`): a `practice2_*` set's first day inserts the same 323 orders / 636 items /
404 payments / 1,756 events as before, plus 3 warehouses and 4,434 stock movements, `29 passed`; the reader grants
below were run as written, with non-superuser `ecomgen_practice` / `ecomgen_reader` roles.

## What goes where (practice vs benchmark)

| | Practice dataset (this runbook) | Release benchmark (leave alone) |
|---|---|---|
| Database | `ecommerce_oltp` (owner `ecomgen_practice`) | `ecommerce_homelab_smoke`, `_test`, `_crash` (owner `ecomgen_app`) |
| Schemas | `practice_commerce` (tables, inventory), `practice_integration` (outbox), `practice_meta` (run ledger) | `commerce`, `integration`, `generator_meta` in its own databases |
| Files | `/srv/homelab/ecommerce/data/practice-pipeline` | `/srv/homelab/ecommerce/data/` (benchmark subfolders) |
| Config / env | `/srv/homelab/ecommerce/practice/pipeline.yaml`, `pipeline.env` | set by `scripts/homelab_benchmark.sh` |

The practice dataset never shares a database, schema or folder with the benchmark. Your older practice run
(`commerce` / `integration` / `generator_meta` in `ecommerce_oltp`, files in `data/practice`) stays as it is: the new
schemas sit next to it and nothing here changes it. Rule for every further dataset: **all three** schema names new,
and its own data folder.

## What you get

| Source | Where | Changes how |
|---|---|---|
| Master data | `practice_commerce.sellers`, `categories`, `customers`, `products` | written once (first step) |
| Orders | `practice_commerce.orders`, `order_items`, `payment_attempts` | new orders every day; `status` / `state` and `updated_at` change as orders progress |
| Outbox (change log) | `practice_integration.outbox_events` | one append-only row per state change, same transaction as the row |
| Warehouses | `practice_commerce.warehouses` | written once (first step) |
| Stock | `practice_commerce.inventory` (current), `inventory_movements` (append-only log) | opening receipts in the first step; weekly deliveries; reserve / release / ship as orders progress (v0.5.0 on) |
| Settlement files | `data/practice-pipeline/source_feeds/payment_settlement/date=D/*.csv` | one set per day a payment settles |
| Clickstream files | `data/practice-pipeline/clickstream/date=D/*.jsonl` | browsing + `order_placed` events per day |
| Manifests | `data/practice-pipeline/manifests/run_id=R/manifest.json` | one per step: files, sha256, record counts, time window |
| Run ledger | `practice_meta.generation_runs` | one row per step: `previous_as_of`, `as_of`, state |

The simulated clock covers **2026-01-01 to 2026-02-01** (10,000 orders, 100,000 browsing events). Each clock step
moves it forward: rows, outbox events and files of the window `[previous step, this step)`, all in UTC.

## Before you start

Needs release **`v0.5.0-homelab`** or later (inventory; `v0.4.x` gives everything but warehouses and stock). Check
on the server:

```bash
cd /srv/homelab/projects/ecommerce-homelab && git describe --tags --always
```

If it shows an older tag, upgrade the code first (changes only the code checkout; your databases and files are not
touched; going back = `git checkout <old tag> && uv sync --frozen`):

```bash
cd /srv/homelab/projects/ecommerce-homelab
git fetch --tags origin
git checkout v0.5.0-homelab
uv sync --frozen
git describe --tags --always
```

Also confirm the NVMe is mounted (`findmnt /srv/homelab` shows `rw`).

**A practice set you already started under `v0.4.x`** (data version 5) keeps working for reading, `status` and
`validate`, but `v0.5.0` won't add days to it (`data_version … differ from the chain's`): a chain stays on the data
version it started with. Either keep advancing it with `v0.4.1-homelab` checked out, or start a new set with its own
three schema names and folder (see "Another dataset"; the same seed gives the same orders, now with stock).

## Step 1: config, data folder, connection file (changes: 3 new files / folders)

```bash
cd /srv/homelab/ecommerce/practice
ls pipeline.yaml pipeline.env 2>/dev/null && echo "EXISTS - stop"
cp --update=none /srv/homelab/projects/ecommerce-homelab/config/practice.example.yaml pipeline.yaml
mkdir /srv/homelab/ecommerce/data/practice-pipeline
(umask 077; sed 's#^ECOMGEN_DATA_ROOT=.*#ECOMGEN_DATA_ROOT=/srv/homelab/ecommerce/data/practice-pipeline#' practice.env > pipeline.env)
grep -n "_schema:\|seed:" pipeline.yaml
grep -o '^[A-Z_]*=' pipeline.env
stat -c '%a %n' pipeline.env
```

Expected: no `EXISTS`; schemas `practice_commerce`, `practice_integration`, `practice_meta`; `pipeline.env` holds
`ECOMMERCE_DATABASE_URL=` and `ECOMGEN_DATA_ROOT=` (values not printed), mode `600`. It reuses your
`ecomgen_practice` password from `practice.env`.

Optional, before the first step only: change `seed:` in `pipeline.yaml` for different data (the same seed always
gives the same data). Don't edit the file after the first step: every step must use the same config.

## Step 2: load the connection and create the schemas (changes: 3 new schemas in `ecommerce_oltp`)

Do this in every new terminal before using the dataset:

```bash
cd /srv/homelab/projects/ecommerce-homelab
set -a; . /srv/homelab/ecommerce/practice/pipeline.env; set +a
C=/srv/homelab/ecommerce/practice/pipeline.yaml
echo "$ECOMMERCE_DATABASE_URL" | sed 's#//\([^:]*\):[^@]*@#//\1:***@#'
echo "$ECOMGEN_DATA_ROOT"
```

Expected: `postgresql://ecomgen_practice:***@127.0.0.1:5432/ecommerce_oltp` and
`/srv/homelab/ecommerce/data/practice-pipeline`.

Then, once:

```bash
uv run ecomgen check-config $C
uv run ecomgen init-db $C
```

Expected: `config OK`; `applied: 0001_core_schema.sql, … 0010_inventory.sql` (all ten, into the new `practice_*`
schemas only).

## Step 3: the first business day (changes: the practice schemas and folder)

```bash
uv run ecomgen advance $C --to 2026-01-02T00:00:00Z
uv run ecomgen validate $C | tail -1
```

Expected: `step run_… COMPLETED: clock start -> 2026-01-02T00:00:00+00:00`, `inserted sellers=100 categories=40
customers=5000 products=2000 orders=323 … warehouses=3 inventory_movements=4434 outbox_events=1756` (default seed);
`29 passed, 0 failed`.

## Step 4: one more day, any time you like (changes: the practice dataset)

This moves the clock exactly one day past the latest step, so you can run it once per "business day" while you
build and test your pipeline:

```bash
NEXT=$(psql "$ECOMMERCE_DATABASE_URL" -XAtc "SELECT to_char(max(as_of) AT TIME ZONE 'UTC' + interval '1 day', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') FROM practice_meta.generation_runs")
echo "advancing to $NEXT"
uv run ecomgen advance $C --to "$NEXT"
uv run ecomgen validate $C | tail -1
```

Each run prints the orders inserted and updated. Running it twice for the same day is safe (the second says
"a completed step already reached …"). After `2026-02-01T00:00:00Z` the clock is at its end; for more data, start a
second dataset (see "Another dataset" below). To jump ahead several days at once: `--to 2026-01-15T00:00:00Z`; to
finish the month: `--to end`.

If a step is interrupted (Ctrl+C, reboot), the next `advance` refuses; run `uv run ecomgen resume $C`, then
continue.

## Step 5: read it from your pipeline

**Connection.** PostgreSQL listens on `127.0.0.1:5432` on the server only. From the Mac use an SSH tunnel
(`ssh -N -L 15432:127.0.0.1:5432 serveradmin@<server>`, then host `127.0.0.1`, port `15432`, database
`ecommerce_oltp`). For a pipeline, a **read-only** role is the safe choice (changes PostgreSQL; once):

```bash
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -c "CREATE ROLE ecomgen_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 5" -c "GRANT CONNECT ON DATABASE ecommerce_oltp TO ecomgen_reader"
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d ecommerce_oltp -c "GRANT USAGE ON SCHEMA practice_commerce, practice_integration, practice_meta TO ecomgen_reader" -c "GRANT SELECT ON ALL TABLES IN SCHEMA practice_commerce, practice_integration, practice_meta TO ecomgen_reader"
sudo -u postgres psql -X -c '\password ecomgen_reader'
```

(If `ecomgen_reader` already exists from the user guide, skip the `CREATE ROLE` line and run only the second line.)

**Keys and timestamps** (all `timestamptz`, UTC):

| Table | Key | Use for incremental loads |
|---|---|---|
| `customers`, `products`, `sellers`, `categories` | `customer_id`, … | static after the first step |
| `orders` | `order_id` | `placed_at` (new), `updated_at` (changed) |
| `order_items` | `order_item_id` (`order_id` FK) | arrive with their order; never change |
| `payment_attempts` | `payment_id` (`order_id` FK) | `created_at` (new), `updated_at` (changed) |
| `practice_integration.outbox_events` | `event_id` | `occurred_at`; per aggregate `aggregate_seq` |
| `warehouses` | `warehouse_id` | static after the first step |
| `inventory` | (`warehouse_id`, `product_id`) | `updated_at` (latest movement); a snapshot of current stock |
| `inventory_movements` | `movement_id` | `occurred_at` (append-only; `order_item_id` for order movements) |

**Watermark pattern** (what changed since your last load): take the previous step's `as_of` from
`practice_meta.generation_runs` (or your own high-water mark) and select rows with `updated_at >= :watermark` (new and
changed) and `< :now`. The count equals the step's inserted + updated orders (rehearsal: 323 + 259 = 582).

```sql
-- the latest step's window
SELECT previous_as_of AS watermark, as_of AS now FROM practice_meta.generation_runs ORDER BY as_of DESC LIMIT 1;
-- orders new or changed in that window
SELECT order_id, status, placed_at, updated_at, total FROM practice_commerce.orders
WHERE updated_at >= '2026-01-03T00:00:00Z' AND updated_at < '2026-01-04T00:00:00Z' ORDER BY updated_at LIMIT 20;
```

**Change-data-capture pattern** (the outbox): read `outbox_events` in `(occurred_at, event_id)` order from your last
position; each row says what changed (`payload->>'from'` → `payload->>'to'`). Replaying one aggregate's events by
`aggregate_seq` rebuilds its current state; `validate` proves that for every order and payment.

```sql
SELECT event_id, event_type, aggregate_id, payload->>'from' AS from_status, payload->>'to' AS to_status, occurred_at
FROM practice_integration.outbox_events
WHERE occurred_at >= '2026-01-03T00:00:00Z' ORDER BY occurred_at, event_id LIMIT 20;
```

**Stock.** `inventory` is the current state; `inventory_movements` is its log, so stock as of any past time is the
sum of the movements before it, and `on_hand - reserved` (available) is never negative:

```sql
SELECT warehouse_id, sum(on_hand_delta) AS on_hand, sum(reserved_delta) AS reserved
FROM practice_commerce.inventory_movements WHERE product_id = 'prd_42' AND occurred_at < '2026-01-03T00:00:00Z'
GROUP BY 1 ORDER BY 1;
```

**Files.** Each day's files are under `date=YYYY-MM-DD` partitions. Read a step's manifest first: it lists every file
with its `sha256`, size and record count, so your loader can verify before loading
(`sha256sum <file>` must match). A file is never rewritten once published. To practise late or duplicated
deliveries, copy one day's files into `deliveries/` with simulated arrival times (a day-aligned step holds its day
whole):

```bash
R=$(psql "$ECOMMERCE_DATABASE_URL" -XAtc "SELECT run_id FROM practice_meta.generation_runs WHERE previous_as_of = '2026-01-02T00:00:00Z'")
uv run ecomgen replay-files $C --run-id "$R" --from 2026-01-02 --to 2026-01-02 --duplicate-pct 5
```

## Checking on it

```bash
uv run ecomgen status $C          # latest step: state, clock window, progress (read-only)
uv run ecomgen validate $C        # 29 checks: rows, ledger, outbox history, stock, files (read-only)
psql "$ECOMMERCE_DATABASE_URL" -XAtc "SELECT count(*), max(as_of) FROM practice_meta.generation_runs"
du -sh /srv/homelab/ecommerce/data/practice-pipeline
```

## Another dataset (keep this one)

Never reuse or empty a schema set: IDs like `ord_1` are deterministic and would collide. For a second dataset copy
`pipeline.yaml` to a new name, change **all three** schema names (e.g. `practice2_commerce`, `practice2_integration`,
`practice2_meta`) and use a new data folder and env file, then repeat steps 1-4. `init-db` refuses an integration
schema that already belongs to another dataset.

**Let `ecomgen_reader` read the new set** (changes PostgreSQL permissions; once, after its first step). The reader
role only sees schemas it was granted, so a new set needs three grants, run as the set's owner `ecomgen_practice`
(the URL in its env file), not as the PostgreSQL superuser:

- `GRANT USAGE ON SCHEMA` lets the role look inside the schemas (name lookup only; no data, no create rights);
- `GRANT SELECT ON ALL TABLES IN SCHEMA` lets it read the tables that exist **now**;
- `ALTER DEFAULT PRIVILEGES … GRANT SELECT ON TABLES` lets it read tables `ecomgen_practice` creates there **later**
  (a future release's `init-db` adding a table), so you don't have to grant again.

Print the three schema names from the new config and use exactly those (the example uses `practice2_*`):

```bash
grep "_schema:" /srv/homelab/ecommerce/practice/pipeline2.yaml
```

<!-- reader-grants: tests/integration/test_reader_role.py runs these statements -->
```bash
psql "$ECOMMERCE_DATABASE_URL" -X -v ON_ERROR_STOP=1 \
  -c "GRANT USAGE ON SCHEMA practice2_commerce, practice2_integration, practice2_meta TO ecomgen_reader" \
  -c "GRANT SELECT ON ALL TABLES IN SCHEMA practice2_commerce, practice2_integration, practice2_meta TO ecomgen_reader" \
  -c "ALTER DEFAULT PRIVILEGES IN SCHEMA practice2_commerce, practice2_integration, practice2_meta GRANT SELECT ON TABLES TO ecomgen_reader"
```

Nothing else is granted: no `INSERT` / `UPDATE` / `DELETE` / `TRUNCATE`, no `CREATE` on the schemas, and only the
owner may alter or drop tables. Check as the reader (password from `~/.pgpass`); the first must print a count, the
other two must fail with `permission denied` (each changes nothing even if it ran: `WHERE false`, `ROLLBACK`):

```bash
R=postgresql://ecomgen_reader@127.0.0.1:5432/ecommerce_oltp
psql "$R" -XAtc "SELECT count(*) FROM practice2_commerce.inventory_movements"
psql "$R" -XAtc "UPDATE practice2_commerce.orders SET status = status WHERE false"
psql "$R" -XAtc "BEGIN; CREATE TABLE practice2_commerce.reader_probe (x int); ROLLBACK"
```

The same applies to a set that existed before the default privileges: after an `init-db` that adds tables (e.g.
`v0.5.0` on a `v0.4.x` set), re-run the `GRANT SELECT ON ALL TABLES` line for its schemas (or the
`ALTER DEFAULT PRIVILEGES` line once, before the upgrade).

To remove a practice dataset you no longer want, decide it explicitly (it is your data): drop its three schemas
together and delete its data folder. Dropping only some of them leaves the others behind and `init-db` then refuses.

## Troubleshooting

| Message | Meaning / fix |
|---|---|
| `step run_… is RUNNING: finish it with ecomgen resume first` | the last step was interrupted: `uv run ecomgen resume $C` |
| `a completed step already reached …` | that day is already done; nothing changed |
| `… differ from the chain's …` | the config changed after the first step: restore it, or start another dataset |
| `this schema predates the outbox (migration 0009)` / `inventory (migration 0010)` | run `uv run ecomgen init-db $C` |
| `data_version … differ from the chain's` | the set was started by an older release: see "Before you start" |
| `integration schema … already holds outbox_events of another schema set` | give the config its own three schema names |
| `as_of … is outside the simulation window` | the clock is past `2026-02-01`: the month is complete |
| `event not found` in bash | a `!` inside double quotes; use the commands as written |
