#!/usr/bin/env bash
# Read-only host snapshot, diffed before/after a homelab run to prove nothing outside the ecomgen stack changed.
# Usage: scripts/host_snapshot.sh > before.txt   (later: > after.txt; diff before.txt after.txt)
# Optional: export EXISTING_PG_URL="postgresql://... [postgresql://...]" (env only, never argv; space-separated) adds
# read-only roles, non-default settings, per-database size and write counters for each PostgreSQL server; PG_SERVICE
# (e.g. postgresql@18-main) adds that systemd unit's start time and restart count. Volatile values (time, free
# memory, load) are omitted. Our own path (SNAP_EXCLUDE: the project directory) is not listed; files are listed to
# SNAP_MAXDEPTH levels below SNAP_ROOT (a file deeper than that changes nothing listed; raise it for a fuller check).
set -uo pipefail
cd "$(dirname "$0")/.."
SNAP_ROOT="${SNAP_ROOT:-/srv/homelab}"
SNAP_EXCLUDE="${SNAP_EXCLUDE:-/srv/homelab/ecommerce}"  # space-separated
SNAP_MAXDEPTH="${SNAP_MAXDEPTH:-4}"

section() { printf '\n=== %s ===\n' "$1"; }

section "host"
uname -srm
section "docker containers (name image state)"
docker ps -a --format '{{.Names}} {{.Image}} {{.State}}' 2>/dev/null | sort || echo "docker unavailable"
section "docker container starts (name id started restarts): unchanged = not restarted"
for c in $(docker ps -aq 2>/dev/null); do
  docker inspect -f '{{.Name}} {{.Id}} {{.State.StartedAt}} {{.RestartCount}}' "$c"
done | sort
section "docker volumes (name created)"
for v in $(docker volume ls -q 2>/dev/null | sort); do
  printf '%s %s\n' "$v" "$(docker volume inspect -f '{{.CreatedAt}}' "$v")"
done
section "docker networks (name driver)"
docker network ls --format '{{.Name}} {{.Driver}}' 2>/dev/null | sort
section "docker images (repository:tag id)"
docker image ls --format '{{.Repository}}:{{.Tag}} {{.ID}}' 2>/dev/null | sort
section "compose projects"
docker compose ls -a --format json 2>/dev/null || true
echo
section "listening TCP ports (address:port)"
if command -v ss >/dev/null 2>&1; then
  ss -ltnH 2>/dev/null | awk '{print $4}' | sort -u
else
  netstat -anp tcp 2>/dev/null | awk '/LISTEN/ {print $4}' | sort -u
fi
section "files under $SNAP_ROOT excluding: $SNAP_EXCLUDE (path size mtime)"
if [ -d "$SNAP_ROOT" ]; then
  prune=(-path /nonexistent-anchor -prune -o)  # never empty: bash 3.2 + set -u rejects empty arrays
  for p in $SNAP_EXCLUDE; do prune+=(-path "$p" -prune -o); done
  if find "$SNAP_ROOT" -maxdepth 0 -printf '' 2>/dev/null; then  # GNU find (Ubuntu)
    find "$SNAP_ROOT" -maxdepth "$SNAP_MAXDEPTH" "${prune[@]}" -printf '%p %s %T@\n' 2>/dev/null | sort
  else                                                            # BSD find (macOS)
    find "$SNAP_ROOT" -maxdepth "$SNAP_MAXDEPTH" "${prune[@]}" -exec stat -f '%N %z %m' {} + 2>/dev/null | sort
  fi
else
  echo "$SNAP_ROOT does not exist"
fi
if [ -n "${PG_SERVICE:-}" ]; then
  section "systemd unit $PG_SERVICE (started, restarts)"
  systemctl show -p ActiveEnterTimestamp -p NRestarts -p MainPID "$PG_SERVICE" 2>/dev/null
fi
if [ -n "${EXISTING_PG_URL:-}" ]; then
  section "existing PostgreSQL servers (read-only: roles, settings, database size and write counters)"
  .venv/bin/python scripts/homelab_bench.py pg-stats
fi
