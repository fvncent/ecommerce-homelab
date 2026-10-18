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
    source/       git checkout (detached at the approved tag)
    data/{csv,jsonl,parquet,events}/   file feeds (Phase 2; empty for now)
    checkpoints/  (resume state lives in the database ledger today; empty)
    logs/  benchmarks/  metadata/
  ```
- PostgreSQL: the existing host cluster `18/main` on `127.0.0.1:5432`. No new container, no server setting changes,
  no `pg_hba.conf` edits (the default `scram-sha-256` for `127.0.0.1` is used).
- Role `ecomgen_app`: `LOGIN`, no superuser / createdb / createrole / replication / bypassrls. It owns exactly three
  databases: `ecommerce_homelab_smoke`, `ecommerce_homelab_test`, `ecommerce_homelab_crash`, with `CONNECT` revoked
  from `PUBLIC`. Superuser (`sudo -u postgres psql`) is used only to create them.
- `scripts/homelab_benchmark.sh` never creates or drops a database. Its preflight refuses unless the role is
  unprivileged, owns all three databases and they are empty; it refuses a wrong commit, a dirty tree, a missing
  NVMe mount (UUID + read-write checked), another PostgreSQL major version, and < 10 GB free.
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
git rev-parse HEAD          # must equal the tag's commit reported in docs/SESSION_HANDOFF.md
```

Tools: `git`, `findmnt` (util-linux), GNU time (`sudo apt install time`), `uv` (user-local install, then
`uv sync --frozen`; uv fetches Python 3.12 if the system Python is older).

## Stage 2: PostgreSQL role, databases, environment

```bash
sudo -u postgres psql -v ON_ERROR_STOP=1 <<'SQL'
CREATE ROLE ecomgen_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 10;
CREATE DATABASE ecommerce_homelab_smoke OWNER ecomgen_app;
CREATE DATABASE ecommerce_homelab_test  OWNER ecomgen_app;
CREATE DATABASE ecommerce_homelab_crash OWNER ecomgen_app;
REVOKE CONNECT, TEMPORARY ON DATABASE ecommerce_homelab_smoke, ecommerce_homelab_test, ecommerce_homelab_crash FROM PUBLIC;
SQL
sudo -u postgres psql -c '\password ecomgen_app'      # paste the value from: openssl rand -hex 24
```

`.env` in `source/` (mode 600):

```
ECOMMERCE_DATABASE_URL=postgresql://ecomgen_app:<pw>@127.0.0.1:5432/ecommerce_homelab_test
```

Then `set -a; . ./.env; set +a; uv run ecomgen check-config config/generator.example.yaml`. Migrations are applied
by the benchmark (`init-db` per database), or by hand with `ECOMMERCE_DATABASE_URL` pointing at each database.

## Stage 3-5: snapshot, smoke, benchmark, crash/resume

```bash
export EXISTING_PG_URL="postgresql://ecomgen_app:<pw>@127.0.0.1:5432/postgres"   # read-only stats of the cluster
export PG_SERVICE=postgresql@18-main
bash scripts/host_snapshot.sh > /srv/homelab/ecommerce/benchmarks/before.txt
uv run pytest                                          # unit tests; integration tests listed as skipped
EXPECTED_SHA=<APPROVED_TAG> MOUNT_UUID=<uuid> PG_MAJOR=18 PG_SERVICE=postgresql@18-main \
  bash scripts/homelab_benchmark.sh
bash scripts/host_snapshot.sh > /srv/homelab/ecommerce/benchmarks/after.txt
diff /srv/homelab/ecommerce/benchmarks/before.txt /srv/homelab/ecommerce/benchmarks/after.txt
```

The benchmark writes `summary.md`, `log.txt`, `time_*.txt`, `mem_*.txt` (server cgroup `memory.current` sampled every
0.2 s per phase; includes page cache), `sizes_mvp.txt`, `status_crashed.txt` and `digest_*.txt` to
`benchmarks/<UTC>/`. It exits 0 only if every expectation holds, including **data identical to the macOS baseline**
(`scripts/expected_mvp_digest.txt`; the digest is pinned to UTC and bytewise ordering, so server time zone and
locale don't matter), no sessions or prepared transactions left after the SIGKILL, and resumed data identical to
the clean run. WAL growth is server-wide (other activity on the cluster would add to it). Copy `summary.md` into
`docs/BENCHMARKS.md` via a PR from the Mac.

Expected diff: only the three `ecommerce_homelab_*` databases change (size, write counters) and `ecomgen_app`
appears in the role list if the before snapshot was taken before Stage 2; containers, volumes, ports, settings,
the PostgreSQL unit's start time and files under `/srv/homelab` outside the project path are identical.

## Cleanup and rollback (each needs explicit approval; smallest first)

1. Re-run the benchmark: it refuses non-empty databases, so recreate only ours:
   `sudo -u postgres psql -c 'DROP DATABASE ecommerce_homelab_crash'` then the matching `CREATE DATABASE … OWNER
   ecomgen_app` and `REVOKE` from Stage 2 (same for `_smoke` / `_test`).
2. Code: `git checkout --detach <previous approved tag>` (migrations are forward-only; for a test database,
   rollback = recreate it).
3. Remove everything: drop the three databases, then `DROP ROLE ecomgen_app`; `rm -rf /srv/homelab/ecommerce`.
4. Re-run `scripts/host_snapshot.sh` and diff against `before.txt`.

## Expected resources (macOS rehearsal on PostgreSQL 17, least-privilege role)

| Run | Wall | Generator RSS | PostgreSQL server | DB size | WAL |
|---|---|---|---|---|---|
| Smoke 1k | 0.2-0.5 s | ~45 MB | < 250 MB peak | ~9 MB | ~2 MB |
| MVP 10k | 1.5-3 s | 60-80 MB | < 300 MB peak | ~19 MB | ~16 MB |
| Crash + resume 10k | ~3 s | 50-80 MB | < 300 MB peak | ~19 MB | ~16 MB |

Server memory on Ubuntu is the whole `postgresql@18-main` unit including its page cache, so it can read higher than
the container figures. Total disk well under 1 GB.

## Not covered yet

CSV/JSONL/Parquet feeds and manifests are Phase 2 (`TASKS.md`); `data/` stays empty until then.
