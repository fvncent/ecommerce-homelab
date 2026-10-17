# 06 — Ubuntu Deployment and Operations Runbook

## Resource plan for 16 GB RAM

The following are **planning budgets**, not fixed resource consumption or a recommendation to allocate all simultaneously:

| Service | Indicative memory budget | Guidance |
|---|---:|---|
| Ubuntu kernel, system, cache | 2–3 GB | Leave headroom |
| PostgreSQL | 3–4 GB | Measure total RSS and active clients |
| Python generator | 1–2 GB | Start at 1–2 workers and small batches |
| Streamlit | 0.5–1 GB | Add only after CLI works |
| Airflow | 1–2 GB | Later, in separate stack |
| Broker + Debezium | 2–4 GB | Later, monitor heap and total process RAM |
| Spark | 3–6 GB | Run heavy jobs at different times from generation |

When under pressure, shut down optional services, lower max workers, shorten event windows, reduce database pool size and prefer chunked I/O.

## Suggested persistent paths

```text
/srv/ecommerce-generator      source checkout / Compose project
/srv/ecommerce-data           incoming partner feeds, events, manifests, staging
/srv/postgres-data            PostgreSQL dedicated bind mount if chosen
/srv/pipeline-data            independently operated data engineering outputs
```

Keep database storage, data lake and backups on separate directories and ideally separate physical devices when budget allows. Never consider a backup on the same physical disk a disaster-recovery backup.

## Security baseline

- Use generated synthetic data only. Do not generate real card numbers or real identifying information.
- Put DB password in `.env` not Git; provide `.env.example` with placeholders.
- Create separate PostgreSQL users: generator writer, reader, optional CDC replication user with least privileges.
- Keep PostgreSQL and Streamlit bound to localhost/VPN/LAN behind appropriate firewall; do not publish them openly.
- Do not mount `anomaly_truth` or admin credentials into learner pipeline containers.
- Use image tags/digests, volumes and healthchecks; test backups and restores.
- Add `umask`, file ownership, per-service UID, and output directory write checks.

## Initial deployment sequence

1. Record `lsb_release -a`, CPU, RAM, motherboard, disk type and existing Docker/PostgreSQL state.
2. Check free disk and existing Docker volume locations with `df -h` and `docker system df` if Docker is installed.
3. Choose dedicated `ecommerce_oltp` DB; do not touch any existing personal databases.
4. Create persistent directories with appropriately restricted permissions.
5. Validate `.env`, database URL, output paths and resource limits before writing data.
6. Run migrations then smoke test; check actual DB row counts and file manifests.
7. Create a real backup on another device or remote target; verify restore at least once.
8. Only after correctness: turn on continuous simulation and optionally CDC.

## PostgreSQL and CDC operations

- Batch initialization may use COPY; ongoing transactions should use actual UPDATE/DELETE and business-correct transactional events.
- Logical replication ordinarily requires appropriate publisher configuration such as `wal_level=logical`, replication slots and senders; the **exact** settings depend on PostgreSQL version and connector. See official current docs.
- CDC slots can retain substantial WAL while consumers are down. Monitor `pg_replication_slots`, WAL disk usage, and consumer lag. Check `max_slot_wal_keep_size` behavior before enabling a slot; limits may lead to a slot becoming unusable and needing a resnapshot.
- Don't make unrestricted modifications to a live server's `postgresql.conf` merely to launch the simulator.
- Maintain a clean way to reset **only** the dedicated simulation schemas/database, with explicit confirmation and backup option.

PostgreSQL documentation: https://www.postgresql.org/docs/current/logical-replication-config.html and https://www.postgresql.org/docs/current/runtime-config-replication.html

## Daily operator checklist

- Check host memory and swaps: `free -h`; load and process use: `top` / `htop`.
- Check disk: `df -h`; check mount type and device: `lsblk -o NAME,SIZE,FSTYPE,ROTA,MOUNTPOINTS`.
- Inspect PostgreSQL health, connection count, active transactions and disk/WAL usage.
- Inspect generator active/failed run IDs, last committed checkpoint and last published manifest.
- Validate newest file checksums, counts and unexpectedly large partition files.
- Check backups and snapshot restore status.

## Rollback and recoverability

- Migrations are versioned; support rollback only when explicitly tested.
- Never delete a completed run's output automatically when resuming.
- On broken partial chunks: inspect the run ledger; reconcile database markers with published file manifests; finish/retry by deterministic IDs.
- Keep a configured retention limit for staging, logs, clickstream and old benchmark runs to prevent filling the 2 TB drive.
