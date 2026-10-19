# Generator user guide (Ubuntu homelab)

How to generate a practice dataset and look at it, as an operator. For the release benchmark (which needs empty
databases and is not a way to make datasets) see `docs/HOMELAB_DEPLOYMENT.md`; for file contracts `docs/FEEDS.md`.

Commands marked **verified** were run on the homelab: setup and `generate` on 2026-10-18 with `v0.2.0-homelab`;
upgrade, smoke and clock steps (`advance`) on 2026-10-19 with `v0.3.0-homelab`. Everything is typed by hand in an
SSH session on the server (`ssh serveradmin@<server>`). Copy only what is inside the boxes.

## Where things are

| What | Path / name |
|---|---|
| Code (git clone, keep it unmodified) | `/srv/homelab/projects/ecommerce-homelab` |
| Practice config + connection file | `/srv/homelab/ecommerce/practice/` (`practice-10k.yaml`, `practice.env`, mode 600) |
| Practice files | `/srv/homelab/ecommerce/data/practice/` |
| Practice database / role | `ecommerce_oltp`, owned by `ecomgen_practice` (no superuser, no createdb) |
| Benchmark databases (leave alone) | `ecommerce_homelab_{smoke,test,crash}`, role `ecomgen_app`; files in `/srv/homelab/ecommerce/data/{source_feeds,clickstream,manifests,deliveries}` |
| Disposable v0.3.0 test database (yours to keep or drop) | `ecomgen_v030_smoke` (role `ecomgen_practice`): smoke run in `commerce`, clock-step chain in `commerce_stepped`; files in `data/v030-smoke`, `data/v030-stepped`; config `practice/v030-stepped.yaml` |
| PostgreSQL | host cluster 18 on `127.0.0.1:5432` only. Never 55432 (VLSIM container). |

## Quick start (dataset already set up: generate is done once per schema set)

**Verified.** In a new terminal:

```bash
cd /srv/homelab/projects/ecommerce-homelab
set -a; . /srv/homelab/ecommerce/practice/practice.env; set +a
echo "$ECOMMERCE_DATABASE_URL" | sed 's#:[^:@/]*@#:***@#'     # must end in @127.0.0.1:5432/ecommerce_oltp
uv run ecomgen status /srv/homelab/ecommerce/practice/practice-10k.yaml
uv run ecomgen validate /srv/homelab/ecommerce/practice/practice-10k.yaml | tail -1
psql "$ECOMMERCE_DATABASE_URL"
```

Expected: `run run_20261018T135727Z_72e31d: COMPLETED (profile mvp, …)`, `19 passed, 0 failed` (20 checks from
`v0.3.0-homelab` on, once `init-db` has added migration 0008 to this database), and the prompt
`ecommerce_oltp=>` (`=>` = not a superuser). `\q` leaves psql.

The `set -a; . FILE; set +a` line loads the file into the shell: the leading `. ` (dot, space) is required.

## The CLI (all commands that exist)

`uv run ecomgen --help`; every command takes the config path; `uv run ecomgen <command> --help` for options.

| Command | Does | Writes? |
|---|---|---|
| `check-config CONFIG` | validates the YAML and the environment, prints seed, counts, clock, data root | no |
| `init-db CONFIG` | creates the config's schemas and applies pending migrations (idempotent) | schema only |
| `generate CONFIG [--profile smoke]` | one run: rows into PostgreSQL + files; `--profile smoke` = 1,000 orders, otherwise the config counts | yes |
| `status CONFIG [--run-id R]` | run state, progress, whether a crashed run can be resumed | no |
| `validate CONFIG [--run-id R]` | invariants + ledger + files; exit 0 pass, 1 a check failed, 2 config, 3 no run | no |
| `resume CONFIG [--run-id R]` | finishes a crashed (RUNNING) or FAILED run; config must be byte-identical | yes |
| `replay-files CONFIG --from D --to D [...]` | copies a run's files to `deliveries/` with simulated arrival times | files only |
| `cleanup-files CONFIG (--staging \| --replay-id \| --run-id)` | dry run listing generated files; deletes only with `--delete --confirm TOKEN` | only if confirmed |

What the config controls: `generator.seed`, `simulation.clock_start` / `clock_end` (the date range),
`counts` (customers, products, sellers, orders, clickstream_events), `chunk_rows`, the three schema names under
`postgres:`, and the data root (`ECOMGEN_DATA_ROOT` overrides `filesystem.root`). The database URL comes only from
the environment variable named in `connection_url_env` (never in the YAML).

**Not supported yet:** adding a second run to the same schemas (see "More datasets"), `run_mode`
`incremental`/`continuous` (accepted, ignored), `workers: 2` (accepted, ignored), built-in counts for the `practice` /
`advanced` / `stress` profiles (they use the config counts), Parquet, warehouses/inventory/shipments/returns,
anomalies, AI sources.

## One-time setup (done 2026-10-18; for a new machine or a second database)

**Verified.** Each step changes something: run only what you mean to.

1. Check the names are free (read-only):
   ```bash
   sudo -u postgres psql -X -c "SELECT datname, pg_get_userbyid(datdba) AS owner FROM pg_database ORDER BY 1"
   sudo -u postgres psql -X -c "SELECT rolname, rolsuper, rolcreatedb FROM pg_roles WHERE rolname LIKE 'ecomgen%' ORDER BY 1"
   ```
2. Role and database (`CREATE` fails rather than replacing anything that exists):
   ```bash
   sudo -u postgres psql -X -v ON_ERROR_STOP=1 -c "CREATE ROLE ecomgen_practice LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 10" -c "CREATE DATABASE ecommerce_oltp OWNER ecomgen_practice" -c "REVOKE CONNECT, TEMPORARY ON DATABASE ecommerce_oltp FROM PUBLIC"
   openssl rand -hex 24                                      # the password; keep it in a password manager
   sudo -u postgres psql -X -c '\password ecomgen_practice'
   ```
   A separate role matters: the benchmark refuses to run if `ecomgen_app` owns any other database.
3. Folder, config, connection file:
   ```bash
   mkdir -p /srv/homelab/ecommerce/practice /srv/homelab/ecommerce/data/practice
   cp /srv/homelab/projects/ecommerce-homelab/config/generator.example.yaml /srv/homelab/ecommerce/practice/practice-10k.yaml
   (umask 077; touch /srv/homelab/ecommerce/practice/practice.env)
   echo 'ECOMMERCE_DATABASE_URL=postgresql://ecomgen_practice:PASTE_PASSWORD_HERE@127.0.0.1:5432/ecommerce_oltp' >> /srv/homelab/ecommerce/practice/practice.env
   echo 'ECOMGEN_DATA_ROOT=/srv/homelab/ecommerce/data/practice' >> /srv/homelab/ecommerce/practice/practice.env
   nano /srv/homelab/ecommerce/practice/practice.env          # replace the placeholder; Ctrl+O, Enter, Ctrl+X
   stat -c '%a %U %n' /srv/homelab/ecommerce/practice/practice.env   # 600 serveradmin
   ```
4. Schema, generate, validate:
   ```bash
   cd /srv/homelab/projects/ecommerce-homelab
   set -a; . /srv/homelab/ecommerce/practice/practice.env; set +a
   uv run ecomgen check-config /srv/homelab/ecommerce/practice/practice-10k.yaml
   uv run ecomgen init-db /srv/homelab/ecommerce/practice/practice-10k.yaml      # applied: 0001_… 0008_clock_steps.sql
   uv run ecomgen generate /srv/homelab/ecommerce/practice/practice-10k.yaml     # ~5 s for 10k orders
   uv run ecomgen validate /srv/homelab/ecommerce/practice/practice-10k.yaml     # 20 passed, 0 failed
   ```
   Don't edit the YAML after `generate`: `resume` refuses a changed config.

**Seed:** the same seed and config always give the same data (byte for byte). The example's seed `20261017` gives
the benchmark's data: 100 sellers, 40 categories, 5,000 customers, 2,000 products, 10,000 orders, 18,776 order
items, 12,265 payment attempts, 120 files (31.6 MB). Change `seed:` before generating for a different dataset.

## More datasets (one run per schema set)

`generate` refuses a schema set that already holds a run ("database already holds run …; IDs are deterministic"):
IDs like `ord_1` would collide. Nothing is ever deleted to make room. To add a dataset, give it its own schema
names (same database and data root are fine: file names carry the run id). Example, 100,000 orders:

```bash
cd /srv/homelab/ecommerce/practice
cp practice-10k.yaml practice-100k.yaml
sed -i -e 's/^    orders: 10000$/    orders: 100000/' -e 's/^    customers: 5000$/    customers: 50000/' -e 's/^    products: 2000$/    products: 20000/' -e 's/^    clickstream_events: 100000$/    clickstream_events: 1000000/' practice-100k.yaml
sed -i -e 's/^  application_schema: commerce$/  application_schema: commerce_100k/' -e 's/^  integration_schema: integration$/  integration_schema: integration_100k/' -e 's/^  metadata_schema: generator_meta$/  metadata_schema: generator_meta_100k/' practice-100k.yaml
grep -nE '^    (orders|customers|products|clickstream_events):|_schema:' practice-100k.yaml
```

Then `check-config`, `init-db`, `generate`, `validate` with `practice-100k.yaml`, and query
`commerce_100k.orders` etc. **Not yet run on the homelab:** expect roughly 10x the 10k run (about a minute, about
300 MB of files, about 200 MB in PostgreSQL); memory stays bounded by `chunk_rows`. 1M+ orders waits for Phase 7
profiling. Different schema names per dataset is the benchmark's own approach (`*_pc` schemas), but true
incremental generation (new orders appended to an existing dataset) is a Phase 3 feature, not available yet.

## Clock steps (`advance`; from `v0.3.0-homelab`, verified 2026-10-19)

`generate` writes a dataset as it stands at `clock_end` in one go. `advance` moves a simulation clock through the
same period instead, so the data changes the way a live shop's does. Each step is a run that:

- **inserts** the orders placed since the previous step and **updates** earlier orders and payments whose status
  changed (CONFIRMED → SHIPPED → DELIVERED, CAPTURED → SETTLED) in PostgreSQL;
- then **publishes the files of its window** `(previous step, this step]` (the first step: from `clock_start`):
  settlements whose `settled_at` is in it, `order_placed` events of the orders placed in it, browsing events whose
  `event_time` is in it, and a manifest for the step.

Stepping to the end, in any number of steps, gives exactly the rows `generate` writes and, across all steps' files,
exactly its records (each record in one step only).

Rules: a schema set holds either one `generate` run or one chain of steps, so give the stepped dataset its own
schema names; every step uses the same config (same seed, counts, clock, data root and feeds); the clock only moves
forward, within `clock_start`..`clock_end`.

**Verified** (2026-10-19, in a disposable database `ecomgen_v030_smoke` with the practice role; any database
works). A stepped dataset needs its own config copy (other schema names) and its own data folder:

```bash
cd /srv/homelab/ecommerce/practice
ls v030-stepped.yaml 2>/dev/null && echo "EXISTS - stop"          # never overwrite a config a dataset uses
cp --update=none practice-10k.yaml v030-stepped.yaml
sed -i -e 's/^  application_schema: commerce$/  application_schema: commerce_stepped/' -e 's/^  integration_schema: integration$/  integration_schema: integration_stepped/' -e 's/^  metadata_schema: generator_meta$/  metadata_schema: generator_meta_stepped/' v030-stepped.yaml
grep -n "_schema:" v030-stepped.yaml                               # three *_stepped names
mkdir /srv/homelab/ecommerce/data/v030-stepped
cd /srv/homelab/projects/ecommerce-homelab
set -a; . /srv/homelab/ecommerce/practice/practice.env; set +a
export ECOMMERCE_DATABASE_URL="${ECOMMERCE_DATABASE_URL%/ecommerce_oltp}/ecomgen_v030_smoke"   # omit to use ecommerce_oltp
export ECOMGEN_DATA_ROOT=/srv/homelab/ecommerce/data/v030-stepped
echo "$ECOMMERCE_DATABASE_URL" | sed 's#//\([^:]*\):[^@]*@#//\1:***@#'                           # check the database name
S=/srv/homelab/ecommerce/practice/v030-stepped.yaml
uv run ecomgen init-db $S                                           # applied: 0001_… 0008_clock_steps.sql
uv run ecomgen advance $S --to 2026-01-02T00:00:00Z                 # first step: master data + day 1
for d in 03 04 05 06 07 08; do uv run ecomgen advance $S --to 2026-01-${d}T00:00:00Z; done
uv run ecomgen advance $S --to 2026-01-15T00:00:00Z
uv run ecomgen advance $S --to end
uv run ecomgen validate $S                                          # 20 passed, 0 failed
```

What it printed (10k config): the first step `inserted sellers=100 categories=40 customers=5000 products=2000
orders=322 …`, `updated orders=0`; then each daily step about 320 new orders, with updates growing as earlier orders
progress (`updated orders=119` on day 2 to `578` on day 7); 0.3-0.5 s per daily step, 3.1 s for the last 17 days.
After day 1 no order was DELIVERED and no payment SETTLED (no settlement file yet); after day 7 there were 553
DELIVERED orders and 962 SETTLED payments. At the end all 7 tables matched a one-shot `generate` of the same config
row for row (md5 per table), the settlement records were identical, and the clickstream held 110,000 records.

Each step prints `step run_… COMPLETED: clock <previous> -> <as_of>`, the rows inserted and updated, and its files.
`--to` needs a time zone (`Z` for UTC; other offsets are converted) or `end`. Repeating a step that is already done,
the latest or an earlier one, prints "a completed step already reached …" and changes nothing (no file is
rewritten), so the whole list of commands can be re-run safely. A step killed midway stays RUNNING; `advance` then
refuses until `uv run ecomgen resume CONFIG` finishes it (database rows, then the missing files, then the manifest).
**Verified** with a hard kill (SIGKILL) after a step's rows were committed and before its files: `status` said
"no process is writing: the run crashed"; `validate` failed `run_completed` and `files_match_ledger` (one reason:
the step is RUNNING); `advance` refused (exit 1); `resume` printed `0 chunks written, 1 already committed` and
published the files; `validate` then passed 20/20 and a second `resume` was a no-op.
If a row was changed by hand in between, the step stops ("not as the previous step left them") without writing
that chunk.

**What changed in PostgreSQL** (the chain, then the latest status mix):

```sql
SELECT run_id, state, previous_as_of, as_of, counts->'written' FROM generator_meta_stepped.generation_runs ORDER BY as_of;
SELECT status, count(*) FROM commerce_stepped.orders GROUP BY 1 ORDER BY 2 DESC;
```

`counts->'written'` lists the rows each step inserted per table and `orders_updated` / `payment_attempts_updated`.

**Files of a step:** every file name starts with the step's run id; its manifest is
`manifests/run_id=<step run id>/manifest.json` and carries a `clock_step` block:

```bash
D=/srv/homelab/ecommerce/data/v030-stepped
python3 -m json.tool $D/manifests/run_id=<step run id>/manifest.json | head -20
find $D/source_feeds $D/clickstream -name '<step run id>*' | sort | head
```

```json
"clock_step": {"first_step_run_id": "run_…", "previous_as_of": "2026-01-08T00:00:00Z", "as_of": "2026-01-15T00:00:00Z",
               "window": "(previous_as_of, as_of]", "window_start": "2026-01-08T00:00:00Z"}
```

`validate CONFIG` checks the files of every step together against the current tables (each settled payment in
exactly one settlement row, each order in exactly one `order_placed` event, no event ID in two steps, and every
record inside the window of the step that published it: `records_in_their_step_window`); `--run-id` scopes the
run/ledger checks to one step. While a step is unfinished, its files are reported once ("… is RUNNING: a clock
step's files are checked once it completes") instead of as hundreds of missing records.

**Replaying a step's files:** `replay-files CONFIG --run-id <step run id> --from D --to D` copies that step's files.
A day whose files are split across two steps is refused ("would deliver part of a day"). A step that ends mid-day
splits that day. **Known limitation:** stepping on day boundaries (`--to …T00:00:00Z`) does *not* always keep days
whole. An event exactly at midnight belongs to the step that ends then (windows include their end), but sits in the
next day's partition. On the homelab, one browsing event at `2026-01-08T00:00:00Z` put a file of the Jan 7 → Jan 8
step into `date=2026-01-08`, so replaying Jan 8 from either step is refused. Replay the dates that hold files of
one step only.

**Time conventions (all UTC):** `event_time` / `settled_at` / `placed_at` = when it happened in the simulated
business (a step's window is about these); `arrived_at` (clickstream) = when the web app received the event, 1-30 s
later, so it can fall just after the step's `as_of`; `published_at` (manifest) = when the generator recorded the
file (generation time, wall clock); delivery time = `replay-files`' `arrived=` partition and delivery manifest.
Partitions (`date=`) are the UTC date of the event time.

## Crashes and history

- `status` shows the latest run (`--run-id` for another): `COMPLETED`, `FAILED` (with `last error`), or
  `RUNNING`; for a dead RUNNING run it says "no process is writing: the run crashed; run `ecomgen resume`".
- `resume CONFIG` finishes it: committed chunks are skipped, files are adopted or published; a second resume is a
  no-op. Then `validate`.
- All runs of a schema set:
  ```sql
  SELECT run_id, state, profile, seed, started_at, finished_at, error FROM generator_meta.generation_runs ORDER BY started_at;
  ```

## PostgreSQL in the terminal

**Verified.** After loading `practice.env`: `psql "$ECOMMERCE_DATABASE_URL"`. psql commands start with `\`; SQL
ends with `;`. Long output opens a pager: `q` returns to the prompt, space scrolls.

| Command | Shows |
|---|---|
| `\conninfo` | user, database, host, port |
| `\l ecommerce*` | databases |
| `\dn` | schemas: `commerce`, `generator_meta`, `integration`, `public` |
| `\dt commerce.*` | the 7 source tables |
| `\d+ commerce.orders` | columns, constraints, indexes, foreign keys |
| `\di commerce.*` | indexes |
| `\x` | toggle one-column-per-line display (good for wide rows) |
| `\q` | quit |

Counts:
```sql
SELECT 'sellers' AS t, count(*) FROM commerce.sellers UNION ALL SELECT 'categories', count(*) FROM commerce.categories UNION ALL SELECT 'customers', count(*) FROM commerce.customers UNION ALL SELECT 'products', count(*) FROM commerce.products UNION ALL SELECT 'orders', count(*) FROM commerce.orders UNION ALL SELECT 'order_items', count(*) FROM commerce.order_items UNION ALL SELECT 'payment_attempts', count(*) FROM commerce.payment_attempts;
```

Rows:
```sql
SELECT customer_id, country, status, created_at FROM commerce.customers ORDER BY customer_id LIMIT 5;
SELECT product_id, seller_id, category_id, price, currency FROM commerce.products LIMIT 5;
SELECT o.order_id, o.customer_id, c.country, o.status, o.placed_at, o.currency, o.subtotal, o.discount, o.tax, o.shipping, o.total FROM commerce.orders o JOIN commerce.customers c USING (customer_id) ORDER BY o.placed_at LIMIT 5;
SELECT order_item_id, line_no, product_id, quantity, unit_price, discount, tax, line_total FROM commerce.order_items WHERE order_id = 'ord_1' ORDER BY line_no;
SELECT payment_id, attempt_no, method, state, amount, currency, created_at FROM commerce.payment_attempts WHERE order_id = 'ord_1' ORDER BY attempt_no;
SELECT status, count(*), sum(total) AS revenue FROM commerce.orders GROUP BY status ORDER BY 2 DESC;
```

Relationship checks (each should return 0; `validate` runs these and more):
```sql
SELECT count(*) FROM commerce.orders o WHERE NOT EXISTS (SELECT 1 FROM commerce.customers c WHERE c.customer_id = o.customer_id);
SELECT count(*) FROM (SELECT o.order_id FROM commerce.orders o JOIN commerce.order_items i USING (order_id) GROUP BY o.order_id, o.subtotal HAVING sum(i.quantity * i.unit_price) <> o.subtotal) x;
SELECT count(*) FROM commerce.payment_attempts p JOIN commerce.orders o USING (order_id) WHERE p.state <> 'FAILED' AND p.amount <> o.total;
SELECT count(*) FROM (SELECT order_id FROM commerce.payment_attempts WHERE state <> 'FAILED' GROUP BY order_id HAVING count(*) > 1) x;
```
(Foreign keys, `total = subtotal - discount + tax + shipping` and `line_total = quantity * unit_price - discount +
tax` are enforced by the schema itself, so violating rows cannot exist.)

Sizes and activity:
```sql
SELECT pg_size_pretty(pg_database_size(current_database()));
SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS total FROM pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 10;
SELECT pid, usename, datname, state, now() - query_start AS running_for, left(query, 60) FROM pg_stat_activity WHERE datname = current_database();
```

Avoid `!` inside double-quoted shell commands (bash reports `event not found`); inside psql it is fine.

## GUI from the Mac (DBeaver) over an SSH tunnel

PostgreSQL stays on `127.0.0.1:5432`; nothing is exposed. On the Mac:

- **DBeaver's built-in tunnel:** New connection → PostgreSQL. Main tab: host `127.0.0.1`, port `5432`, database
  `ecommerce_oltp`, user `ecomgen_practice`, its password. SSH tab: "Use SSH tunnel", host = the server, port 22,
  user `serveradmin`, your SSH key.
- **Or a manual tunnel** in a Mac terminal (stays open while you work): `ssh -N -L 15432:127.0.0.1:5432
  serveradmin@<server>`; then in any GUI: host `127.0.0.1`, port `15432`, database `ecommerce_oltp`, user
  `ecomgen_practice`. Port 15432 avoids clashing with PostgreSQL on the Mac.

`ecomgen_practice` is not a superuser but can write its database. For browsing only, a read-only role is safer
(changes PostgreSQL; not created yet):

```bash
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -c "CREATE ROLE ecomgen_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 5" -c "GRANT CONNECT ON DATABASE ecommerce_oltp TO ecomgen_reader"
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d ecommerce_oltp -c "GRANT USAGE ON SCHEMA commerce, integration, generator_meta TO ecomgen_reader" -c "GRANT SELECT ON ALL TABLES IN SCHEMA commerce, integration, generator_meta TO ecomgen_reader" -c "ALTER DEFAULT PRIVILEGES FOR ROLE ecomgen_practice GRANT SELECT ON TABLES TO ecomgen_reader"
sudo -u postgres psql -X -c '\password ecomgen_reader'
```
New schema sets (e.g. `commerce_100k`) need their own `GRANT USAGE ON SCHEMA …` for the reader.

## Files

Layout under the data root (`/srv/homelab/ecommerce/data/practice` for practice):

```text
source_feeds/payment_settlement/date=YYYY-MM-DD/<run_id>-<provider>-part-NNNNN.csv   # settlements, header row
clickstream/date=YYYY-MM-DD/<run_id>-part-NNNNN.jsonl                               # one JSON event per line
manifests/run_id=<run_id>/manifest.json                                             # every file + sha256
deliveries/replay_id=<id>/arrived=YYYY-MM-DD/<original path>                       # replayed copies (*.dup1.* = duplicate)
audit/cleanup_id=<id>.json                                                          # only after a confirmed cleanup
staging/                                                                            # in-progress output; empty when idle
```

Originals are under `source_feeds/` and `clickstream/`; anything under `deliveries/` is a replay copy. A file
belongs to the run whose id starts its name, and the run's manifest lists it.

```bash
D=/srv/homelab/ecommerce/data/practice
ls $D/manifests
du -sh $D/* ; df -h /srv/homelab
find $D/source_feeds -name '*.csv' | sort | head -5
find $D/clickstream -name '*.jsonl' | wc -l
F=$(find $D/source_feeds -name '*.csv' | sort | head -1); head -3 "$F" | column -s, -t
G=$(find $D/clickstream -name '*.jsonl' | sort | head -1); head -2 "$G" | python3 -m json.tool --json-lines
wc -l "$F" "$G"                                     # CSV lines = records + 1 header
python3 -m json.tool $D/manifests/run_id=*/manifest.json | head -40
```

Checksums against the manifest (read-only; `validate` does the same with the database ledger):
```bash
python3 -c 'import glob,hashlib,json; r="/srv/homelab/ecommerce/data/practice"; m=json.load(open(glob.glob(r+"/manifests/run_id=*/manifest.json")[0])); bad=[f["file_path"] for f in m["files"] if hashlib.sha256(open(r+"/"+f["file_path"],"rb").read()).hexdigest() != f["sha256"]]; print(len(m["files"]), "files,", sum(f["records_written"] for f in m["files"]), "records,", len(bad), "mismatches")'
```
(With several runs in the root, replace `run_id=*` with the run's folder.) Or one file:
`sha256sum "$F"` and compare with its `sha256` in the manifest.

Never delete or edit these by hand. `cleanup-files` is the only supported way, dry run first.

## Validation

`uv run ecomgen validate CONFIG` prints one line per check and `N passed, M failed`; exit 0 only if all pass.

| Checks | Prove |
|---|---|
| `run_completed`, `ledger_complete`, `ledger_matches_tables` | the run finished; every planned chunk is recorded; ledger counts = table counts (no incomplete jobs) |
| `order_header_matches_items`, `item_lines_contiguous`, `item_currency_matches_order` | order totals reconcile with their items |
| `payment_amount_matches_total`, `one_live_payment`, `payment_attempts_contiguous`, `order_status_matches_payment` | payments reconcile with orders |
| `timestamps_ordered`, `category_tree_acyclic` | time and hierarchy are consistent |
| `files_match_ledger`, `manifest_matches_ledger` | every file exists with its size, sha256 and record count; manifest = ledger |
| `settlements_match_payments`, `settled_payments_settled_once` | settlement rows = SETTLED payments (order, amount) exactly once |
| `clickstream_ids_exist`, `orders_placed_once`, `events_unique_and_complete` | event IDs reference real customers/products/orders; one `order_placed` per order |

Primary keys, foreign keys and the money formulas are database constraints, so they hold for every row. A failure
prints `FAIL <check>: N violation(s) — <meaning>` and sample rows. Common ones: `run_completed` on a crashed run
(run `resume`), `files_match_ledger` "deleted by cleanup" after a confirmed `cleanup-files --run-id`, or a file
edited/removed by hand.

## Monitoring and sizes

```bash
free -h                      # memory; the 10k run peaks at ~70 MB for the generator
df -h /srv/homelab /         # data goes to /srv/homelab (NVMe) only
du -sh /srv/homelab/ecommerce/data/practice
top                          # or htop if installed; q quits
findmnt /srv/homelab         # must show /dev/nvme0n1p1
```

The data root must already exist, and `/srv/homelab/ecommerce/...` exists only on the mounted NVMe, so an unmounted
drive makes `generate` stop ("data root … does not exist") instead of writing to the system disk.

Measured on this server (Ryzen 5 5600, 16 GB): 10k orders with files in 4.6 s, 68 MB generator RSS, PostgreSQL
~180 MB peak, 20 MB database + 31.6 MB files (`docs/BENCHMARKS.md`). Recommended: 10k for exploring, 100k for
pipeline practice (not yet measured here), larger only after Phase 7.

## Troubleshooting

| Message | Cause / fix |
|---|---|
| `bash: …practice.env: Permission denied` | the `. ` before the path was missing: use `set -a; . FILE; set +a` |
| `!~: event not found` | `!` in a double-quoted shell command: rewrite without `!` |
| `password authentication failed` | password in `practice.env` ≠ the one set with `\password` |
| `database already holds run …` | that schema set is used: new schema names (see "More datasets") |
| `can't resume run …: … differ` | the YAML changed after `generate`: restore it exactly |
| `data root … does not exist` | `mkdir` it, or the NVMe is not mounted (`findmnt /srv/homelab`) |
| `generator schema not found; run ecomgen init-db first` | `init-db` with the same config |
| `another generate/resume holds the run lock` | another run is active; wait, or check `status` |
