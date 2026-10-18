# Homelab Deployment Verification (Ubuntu)

Runbook for deploying an **approved tag from GitHub** to the Ubuntu server and running smoke (1k) → benchmark
(10k) → crash/resume against the host's PostgreSQL 18, proving nothing else changed. Development stays on macOS;
nothing is edited on Ubuntu. The operator runs every command by hand, one step at a time; each step says whether
it is read-only.

## Layout and safety rules

- Project root `/srv/homelab/ecommerce` on the NVMe (`/srv/homelab`, ext4). Nothing else under `/srv/homelab` is
  touched; no recursive `chown`/`chmod`, no `777`.
  ```
  /srv/homelab/ecommerce/
    source/       git checkout at the approved commit (or an existing clone elsewhere: then add it to
                  SNAP_EXCLUDE; the 2026-10-18 run used /srv/homelab/projects/ecommerce-homelab)
    data/{csv,jsonl,parquet,events}/   file feeds (Phase 2; empty for now)
    checkpoints/  (resume state lives in the database ledger today; empty)
    logs/  benchmarks/  metadata/
  ```
- PostgreSQL: the existing host cluster `18/main` on `127.0.0.1:5432`. No new container, no server setting changes,
  no `pg_hba.conf` edits (the default `scram-sha-256` for `127.0.0.1` is used).
- Role `ecomgen_app`: `LOGIN`, no superuser / createdb / createrole / replication / bypassrls, member of no role.
  It owns exactly three databases: `ecommerce_homelab_smoke`, `ecommerce_homelab_test`, `ecommerce_homelab_crash`,
  with `CONNECT` revoked from `PUBLIC`. Superuser (`sudo -u postgres psql`) is used only to create them and for
  read-only snapshots.
- `scripts/homelab_benchmark.sh` never creates or drops a database. Its preflight refuses unless the role has no
  privileges or memberships, owns only those three databases and they are empty; it also refuses a wrong commit, a
  dirty tree, output outside `/srv/homelab/ecommerce` or not on the NVMe mount (UUID + read-write checked), a
  `PG_SERVICE` that isn't the server the URL reaches (our backend PID must be in its cgroup), another PostgreSQL major
  version, and < 10 GB free.
- Other services (VLSIM engine + TMS, PostgreSQL 17 container on 55432, Redpanda) are never stopped, restarted or
  reconfigured. `scripts/host_snapshot.sh` records their container IDs, start times and restart counts.
- Secrets: the role password is set with `\password` (never in shell history or argv); `.env` is mode 600 and
  never committed.
- Integration tests (`pytest` with `ECOMGEN_TEST_DATABASE_URL`) create and drop throwaway databases and need
  `CREATEDB`, so they are **not** run on Ubuntu (Mac + future CI cover them). On Ubuntu: unit tests only.

## Stage 1: directories and checkout

```bash
# read-only: mount + device
findmnt -no SOURCE,FSTYPE,OPTIONS,TARGET /srv/homelab
lsblk -no NAME,MODEL,SIZE,FSTYPE,UUID,MOUNTPOINTS /dev/nvme0n1     # note the UUID -> MOUNT_UUID
# changes (project path only): one top-level directory owned by serveradmin, then subdirectories without sudo
sudo install -d -o serveradmin -g serveradmin -m 750 /srv/homelab/ecommerce
mkdir -p /srv/homelab/ecommerce/{data/{csv,jsonl,parquet,events},checkpoints,logs,benchmarks,metadata}
git clone https://github.com/fvncent/ecommerce-homelab.git /srv/homelab/ecommerce/source
cd /srv/homelab/ecommerce/source && git checkout --detach <APPROVED_TAG>
git rev-parse HEAD          # must equal the approved tag's commit (docs/SESSION_HANDOFF.md, "Deploy target")
```

Tools: `git`, `findmnt` (util-linux), GNU time (`sudo apt install time`), `uv` (user-local install, then
`uv sync --frozen`; uv fetches Python 3.12 if the system Python is older).

## Stage 2: PostgreSQL role, databases, environment

Take the "before" snapshot first, so the role and databases created here show up in the diff:

```bash
cd /srv/homelab/ecommerce/source
sudo PG_LOCAL_SUPERUSER=1 PG_SERVICE=postgresql@18-main SNAP_EXCLUDE="/srv/homelab/ecommerce <CLONE>" \
  bash scripts/host_snapshot.sh > /srv/homelab/ecommerce/benchmarks/before.txt   # read-only; as root it sees
                                                                                  # every file and docker
```

```bash
sudo -u postgres psql -v ON_ERROR_STOP=1 \
  -c "CREATE ROLE ecomgen_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 10" \
  -c "CREATE DATABASE ecommerce_homelab_smoke OWNER ecomgen_app" \
  -c "CREATE DATABASE ecommerce_homelab_test OWNER ecomgen_app" \
  -c "CREATE DATABASE ecommerce_homelab_crash OWNER ecomgen_app" \
  -c "REVOKE CONNECT, TEMPORARY ON DATABASE ecommerce_homelab_smoke, ecommerce_homelab_test, ecommerce_homelab_crash FROM PUBLIC"
# (one -c per statement: a pasted heredoc failed in the first run, so CREATE ROLE never ran)
sudo -u postgres psql -c '\password ecomgen_app'      # paste the value from: openssl rand -hex 24
```

`.env` in `source/` (mode 600):

```
ECOMMERCE_DATABASE_URL=postgresql://ecomgen_app:<pw>@127.0.0.1:5432/ecommerce_homelab_test
ECOMGEN_DATA_ROOT=/srv/homelab/ecommerce/data
```

Then `set -a; . ./.env; set +a; uv run ecomgen check-config config/generator.example.yaml`. Migrations are applied
by the benchmark (`init-db` per database), or by hand with `ECOMMERCE_DATABASE_URL` pointing at each database.

## Stage 3-5: snapshot, smoke, benchmark, crash/resume

```bash
set -a; . ./.env; set +a                               # password comes from .env, never typed on a command line
uv run pytest                                          # unit tests; integration tests listed as skipped
EXPECTED_SHA=<APPROVED_TAG> MOUNT_UUID=<uuid> PG_MAJOR=18 PG_SERVICE=postgresql@18-main EXPECTED_PG_PORT=5432 \
  bash scripts/homelab_benchmark.sh                    # add CLEANUP_REPLAY=1 to really delete the replay it made
sudo PG_LOCAL_SUPERUSER=1 PG_SERVICE=postgresql@18-main SNAP_EXCLUDE="/srv/homelab/ecommerce <CLONE>" \
  bash scripts/host_snapshot.sh > /srv/homelab/ecommerce/benchmarks/after.txt
diff /srv/homelab/ecommerce/benchmarks/before.txt /srv/homelab/ecommerce/benchmarks/after.txt
```

Phases (each check is a PASS/FAIL line in `summary.md`):

1. **Smoke** (1k, database + files) in `ecommerce_homelab_smoke`: validate (database + files), files identical to
   the macOS reference, no orphans or staging left.
2. **Publish crash** in the same database but its own, empty schema set (`commerce_pc`, `integration_pc`,
   `generator_meta_pc`, from a derived config in the output directory: a clean baseline with no reset): SIGKILL
   right after a file is in place, before its ledger row commits → resume → validate; files identical to the
   uninterrupted reference, database rows identical to the smoke run, ledger == disk.
3. **MVP** (10k) in `ecommerce_homelab_test`: time, memory, CPU, WAL, sizes, per-feed file counts/sizes, sha256
   re-verification of every file (MB/s), database rows and files identical to the macOS references.
4. **Feed-only throughput:** each feed alone (no database) into `benchmarks/<UTC>/feedbench/`: records/s, MB/s,
   peak RSS.
5. **Load crash** in `ecommerce_homelab_crash`: SIGKILL before orders chunk 1 → resume; rows and files identical
   to the clean run.
6. **Replay** of the MVP run's files (2026-01-05..10, delay 24 h, jitter 6 h, 10 % duplicates): SIGKILL after
   delivery 6 → rerun → exactly the deterministic plan; a second rerun only verifies; the replayed originals
   (paths, sha256) match the macOS reference. Arrival times and duplicates are seeded by the replay id, which
   includes the run id, so they are compared with the plan on the same machine, not across machines.
7. **Cleanup of that replay:** only if the replay was created by this run (same run id, started after the
   benchmark began); the dry-run list goes to `cleanup_plan.txt` and must contain only that replay's copies. Real
   deletion only with `CLEANUP_REPLAY=1` and only if every earlier check passed; then originals must still match
   and the run still validate.

Pre-flight refuses a non-local server, port 55432 (the VLSIM container), another port than `EXPECTED_PG_PORT`,
`ECOMGEN_DATA_ROOT` outside `/srv/homelab/ecommerce` or off the NVMe mount, a filesystem without hard links, a
config without both feeds, missing references, and non-empty databases (both schema sets). It exits 0 only if
every expectation holds; WAL growth is server-wide. Copy `summary.md` into `docs/BENCHMARKS.md` via a PR from the
Mac. Everything it writes stays under `/srv/homelab/ecommerce` (`benchmarks/<UTC>/`, `data/`), which the snapshot
excludes, so the before/after diff shows only changes outside the project scope.

Expected diff (rehearsed on PostgreSQL 18.6): `ecomgen_app` added (`super=false createdb=false`), three `acl`
lines (owner `ecomgen_app`, only it may connect) and three `db` lines (sizes, write counters). Unchanged: other
roles and memberships, all non-default settings (read as superuser, so complete), the `postgres` database, the
PostgreSQL unit's start time, container IDs / start times / restart counts, volumes, ports, and every file under
`/srv/homelab` outside the project path. Check the snapshot's "unreadable paths" count: changes inside paths it
couldn't read are not seen (run as a user that can read them for a fuller check). If it says `DOCKER UNAVAILABLE`,
the container sections prove nothing; stop and decide how to read them (a system change, so it needs approval).

## Phase 2 release (`v0.2.0-homelab`): re-run on Ubuntu

The databases hold the 2026-10-18 `v0.1.1-homelab` benchmark, so they must be recreated (approved in principle on
2026-10-18; each command still needs your go-ahead when shown).

1. **Release check (Mac / GitHub):** the tag exists and points at the reviewed `main` commit with CI green.
   On Ubuntu: `git fetch --tags && git rev-parse 'v0.2.0-homelab^{commit}'` must print that SHA.
2. **Inventory, read-only** (what would be dropped):
   ```bash
   sudo -u postgres psql -X -c "SELECT datname, pg_get_userbyid(datdba) AS owner, pg_size_pretty(pg_database_size(datname)) AS size FROM pg_database WHERE datname LIKE 'ecommerce_homelab_%' ORDER BY 1"
   for d in smoke test crash; do sudo -u postgres psql -X -d ecommerce_homelab_$d -c "SELECT run_id, state, profile, started_at FROM generator_meta.generation_runs" -c "SELECT nspname FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema' ORDER BY 1"; done
   sudo -u postgres psql -X -c "SHOW port"            # must be 5432 (never the VLSIM container on 55432)
   findmnt -no SOURCE,UUID,OPTIONS /srv/homelab        # the Samsung NVMe, rw
   ```
   Expected: owner `ecomgen_app`, ~10-20 MB each, one COMPLETED run each, schemas `commerce`, `generator_meta`,
   `integration`, `public` only.
3. **Recreate (destructive, only after you approve the inventory)**, one database at a time:
   ```bash
   sudo -u postgres psql -v ON_ERROR_STOP=1 -c "DROP DATABASE ecommerce_homelab_smoke" -c "CREATE DATABASE ecommerce_homelab_smoke OWNER ecomgen_app" -c "REVOKE CONNECT, TEMPORARY ON DATABASE ecommerce_homelab_smoke FROM PUBLIC"
   ```
   and the same with `_test` and `_crash`.
4. **Deploy:** in the clone, `git checkout --detach v0.2.0-homelab && uv sync --frozen`; add
   `ECOMGEN_DATA_ROOT=/srv/homelab/ecommerce/data` to `.env`. The empty `data/csv`, `jsonl`, `parquet`, `events`
   directories from Stage 1 are unused (the generator writes `source_feeds/`, `clickstream/`, `manifests/`,
   `deliveries/`, `audit/`); remove them by hand if you like.
5. **Run:** snapshot before → benchmark (Stage 3-5 above, `EXPECTED_SHA=v0.2.0-homelab`) → snapshot after → diff.
   `v0.1.1-homelab` stays tagged as the previous working release.

## Cleanup and rollback (each needs explicit approval; smallest first)

1. Re-run the benchmark: it refuses non-empty databases, so recreate only ours:
   `sudo -u postgres psql -c 'DROP DATABASE ecommerce_homelab_crash'` then the matching `CREATE DATABASE … OWNER
   ecomgen_app` and `REVOKE` from Stage 2 (same for `_smoke` / `_test`).
2. Code: `git checkout --detach <previous approved tag>` (migrations are forward-only; for a test database,
   rollback = recreate it).
3. Remove everything: drop the three databases, then `DROP ROLE ecomgen_app`; `rm -rf /srv/homelab/ecommerce`.
4. Re-run `scripts/host_snapshot.sh` and diff against `before.txt`.

## Expected resources (macOS rehearsals on PostgreSQL 17.11 and 18.6, least-privilege role)

| Run | Wall | Generator RSS | PostgreSQL server | DB size | WAL |
|---|---|---|---|---|---|
| Smoke 1k | 0.2-0.5 s | ~45 MB | < 250 MB peak | ~9 MB | ~2 MB |
| MVP 10k | 1.5-3 s | 60-80 MB | < 300 MB peak | ~19 MB | ~16 MB |
| Crash + resume 10k | ~3 s | 50-80 MB | < 300 MB peak | ~19 MB | ~16 MB |

Server memory on Ubuntu is the whole `postgresql@18-main` unit including its page cache, so it can read higher than
the container figures. Total disk well under 1 GB.

## Not covered yet

Parquet snapshots, supplier/courier feeds and age-based retention (see `docs/FEEDS.md`, "Not yet").
