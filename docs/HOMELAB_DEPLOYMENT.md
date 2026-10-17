# Homelab Deployment Verification (Ubuntu)

Runbook for the approved plan: deploy an **approved commit from GitHub** (`main` at a tag) to the Ubuntu server, run
the smoke + 10k benchmark + crash/resume, and prove nothing else on the host changed. Development stays on macOS.
Nothing here runs without explicit approval; every step is non-destructive except the rollback steps marked as such.

## Safety rules

- Never touch `ecommerce_oltp` or any existing database, container, volume or file. The stack is scoped by
  `COMPOSE_PROJECT_NAME=ecomgen_homelab` (container `ecomgen_homelab-postgres-1`, volume `ecomgen_homelab_pgdata`).
- Databases used, all inside our container: `ecommerce_homelab_test` (kept), `ecommerce_homelab_test_smoke` and
  `ecommerce_homelab_test_crash` (dropped by the script, also on abort), `ecomgen_test_*` (pytest throwaways).
  `scripts/homelab_bench.py` hard-codes that namespace: it creates only `ecommerce_homelab_test[_*]`, drops only
  `ecommerce_homelab_test_*`, refuses existing databases and URLs that hide their database (`?dbname=`, `%`).
- Preflight proves `ECOMGEN_TEST_DATABASE_URL` reaches **our** container (same cluster `system_identifier`), so a
  wrong URL can't create or drop anything on another PostgreSQL.
- Database URLs (passwords) are passed via environment variables only, never on a command line.
- PostgreSQL binds to `127.0.0.1` only. Fresh password generated on the server; `.env` is mode 600, never committed.
- A host snapshot is taken before anything starts and diffed afterwards.

## Step 0: read-only inventory (paste the output for review)

```bash
hostnamectl; lsb_release -a; uname -r; nproc; free -h
lsblk -o NAME,SIZE,ROTA,FSTYPE,MOUNTPOINTS; df -h / /srv /var/lib/docker 2>/dev/null
docker version --format '{{.Server.Version}}'; docker compose version
docker info --format 'root={{.DockerRootDir}} cgroup={{.CgroupVersion}}'
docker ps -a --format '{{.Names}} {{.Image}} {{.Ports}}'; docker volume ls; docker compose ls
sudo ss -ltnp | grep -E ':(5432|5433)\b' || echo "5432/5433 free"
ls -la /srv; sudo find /srv/ecommerce-data -maxdepth 2 2>/dev/null | head -20
command -v git uv python3; /usr/bin/time --version 2>&1 | head -1   # GNU time required: sudo apt install time
```

Gates: at least 10 GB free on the Docker root and `/srv`; chosen port free (else `POSTGRES_PORT=5434`); no
`ecomgen_homelab` project/volume yet; cgroup v2 (for container peak memory).

## Step 1: checkout (read-only deploy key for the private repo)

```bash
sudo mkdir -p /srv/ecommerce-generator /srv/ecommerce-data/benchmarks
sudo chown "$USER": /srv/ecommerce-generator /srv/ecommerce-data /srv/ecommerce-data/benchmarks
chmod 750 /srv/ecommerce-data
git clone git@github.com:fvncent/ecommerce-homelab.git /srv/ecommerce-generator
cd /srv/ecommerce-generator && git checkout --detach <APPROVED_TAG_OR_SHA>
uv sync --frozen
```

## Step 2: environment

```bash
cp .env.example .env && chmod 600 .env
# Edit .env:
#   POSTGRES_PASSWORD=<openssl rand -hex 24>
#   POSTGRES_PORT=5433
#   POSTGRES_DB=postgres                      # creates no extra database; the benchmark creates its own
#   COMPOSE_PROJECT_NAME=ecomgen_homelab
#   ECOMMERCE_DATABASE_URL=postgresql://ecomgen:<pw>@127.0.0.1:5433/ecommerce_homelab_test
#   ECOMGEN_TEST_DATABASE_URL=postgresql://ecomgen:<pw>@127.0.0.1:5433/postgres
set -a; . ./.env; set +a
uv run ecomgen check-config config/generator.example.yaml
```

## Step 3: snapshot, start, test, benchmark

```bash
# Optional: export EXISTING_PG_URL=postgresql://<read-only user>@<host>:<port>/postgres adds read-only size and
# user-table write counters for the PostgreSQL that holds ecommerce_oltp (proves it was untouched).
bash scripts/host_snapshot.sh > /srv/ecommerce-data/benchmarks/before.txt
docker compose up -d --wait
uv run pytest && uv run mypy                       # expect: all passed, 0 skipped
EXPECTED_SHA=<APPROVED_TAG_OR_SHA> bash scripts/homelab_benchmark.sh
bash scripts/host_snapshot.sh > /srv/ecommerce-data/benchmarks/after.txt
diff /srv/ecommerce-data/benchmarks/before.txt /srv/ecommerce-data/benchmarks/after.txt
```

Expected diff: only our container, volume, network, the postgres image (if newly pulled) and port 5433 appear;
`/srv/ecommerce-generator` and the benchmark output are excluded from the file listing. If `EXISTING_PG_URL` is set,
each database's `user_inserted/updated/deleted` are identical (the proof of "untouched"). Size can move slightly from
autovacuum shortly after a database was written, so it is supporting evidence only.

The benchmark writes `summary.md`, `log.txt`, `time_*.txt`, `mem_*.txt` (container `memory.current` sampled every
0.2 s per phase), `sizes_mvp.txt` and `digest_*.txt` to `/srv/ecommerce-data/benchmarks/<UTC>/`. It exits 0 only if every expectation holds,
including **data identical to the macOS baseline** (`scripts/expected_mvp_digest.txt`, a cross-platform
determinism check). Copy `summary.md` into `docs/BENCHMARKS.md` via a PR from the Mac.

## Rollback (smallest first)

1. Pause, keep data: `docker compose stop`
2. Drop only our test databases (inside our container):
   `docker compose exec postgres psql -U ecomgen -d postgres -c 'DROP DATABASE ecommerce_homelab_test'`
3. Code: `git checkout --detach <previous approved tag>` (migrations are forward-only; for a test database,
   rollback = drop + re-create).
4. **Destructive, needs explicit approval:** `docker compose down -v` (removes only `ecomgen_homelab_pgdata`),
   `rm -rf /srv/ecommerce-generator`, remove `/srv/ecommerce-data/benchmarks/<run>`.
5. Re-run `scripts/host_snapshot.sh` and diff against `before.txt`.

## Expected resources (from the macOS baseline)

| Run | Wall | Generator RSS | PostgreSQL container | DB size | WAL |
|---|---|---|---|---|---|
| Smoke 1k | 0.2-0.5 s | ~45 MB | < 250 MB peak | ~2-3 MB (dropped) | ~2 MB |
| MVP 10k | 1.5-3 s | 60-80 MB | < 300 MB peak | ~19 MB | ~16 MB |
| Crash + resume 10k | ~3 s | 50-80 MB | < 300 MB peak | ~19 MB (dropped) | ~16 MB |
| pytest | 20-30 s | ~100 MB | < 300 MB peak | throwaway | small |

Total disk under 1 GB including the PostgreSQL image (~430 MB).

## Not covered yet

CSV/JSONL feeds and manifests are Phase 2 (`TASKS.md`); `/srv/ecommerce-data` currently holds benchmark results only.
