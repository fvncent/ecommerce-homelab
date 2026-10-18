#!/usr/bin/env bash
# Read-only host snapshot, diffed before/after a homelab run to prove nothing outside the ecomgen stack changed.
# Usage: scripts/host_snapshot.sh > before.txt   (later: > after.txt; diff before.txt after.txt)
# Optional:
# - PG_LOCAL_SUPERUSER=1: read-only roles, memberships, all non-default settings, per-database size and user-table
#   write counters of the local cluster, as the postgres superuser via `sudo -u postgres psql` (peer auth, no
#   password; every session read-only). Works before our role exists, so the "before" snapshot precedes Stage 2.
# - EXISTING_PG_URL="postgresql://... [postgresql://...]" (env only, never argv; space-separated): the same per
#   server via a URL (settings limited to what that role may see).
# - PG_SERVICE (e.g. postgresql@18-main): that systemd unit's start time and restart count.
# Volatile values (time, free memory, load) are omitted. Our own path (SNAP_EXCLUDE: the project directory) is not
# listed. Files are listed at every depth (SNAP_MAXDEPTH limits it; then deeper changes are not seen); paths this
# user cannot read are counted, since changes inside them are not seen either.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
SNAP_ROOT="${SNAP_ROOT:-/srv/homelab}"
SNAP_EXCLUDE="${SNAP_EXCLUDE:-/srv/homelab/ecommerce}"  # space-separated
SNAP_MAXDEPTH="${SNAP_MAXDEPTH:-}"

section() { printf '\n=== %s ===\n' "$1"; }

section "host"
uname -srm
section "docker containers (name image state)"
docker info >/dev/null 2>&1 || echo "DOCKER UNAVAILABLE to this user: docker sections below prove nothing"
docker ps -a --format '{{.Names}} {{.Image}} {{.State}}' 2>/dev/null | sort
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
  opts=(-path /nonexistent-anchor -prune -o)  # never empty: bash 3.2 + set -u rejects empty arrays
  [ -z "$SNAP_MAXDEPTH" ] || opts=(-maxdepth "$SNAP_MAXDEPTH" "${opts[@]}")
  for p in $SNAP_EXCLUDE; do opts+=(-path "$p" -prune -o); done
  errs="$(mktemp)"
  if find "$SNAP_ROOT" -maxdepth 0 -printf '' 2>/dev/null; then  # GNU find (Ubuntu)
    find "$SNAP_ROOT" "${opts[@]}" -printf '%p %s %T@\n' 2>"$errs" | sort
  else                                                            # BSD find (macOS)
    find "$SNAP_ROOT" "${opts[@]}" -exec stat -f '%N %z %m' {} + 2>"$errs" | sort
  fi
  echo "unreadable paths (contents not checked): $(grep -c . "$errs")"
  sed 's/^find: //' "$errs" | sort | head -20
  rm -f "$errs"
else
  echo "$SNAP_ROOT does not exist"
fi
if [ -n "${PG_SERVICE:-}" ]; then
  section "systemd unit $PG_SERVICE (started, restarts)"
  systemctl show -p ActiveEnterTimestamp -p NRestarts -p MainPID "$PG_SERVICE" 2>/dev/null
fi
if [ "${PG_LOCAL_SUPERUSER:-}" = 1 ]; then
  section "local PostgreSQL cluster as superuser (read-only: roles, memberships, settings, databases)"
  psu() { sudo -u postgres env PGOPTIONS='-c default_transaction_read_only=on' psql -X -At -v ON_ERROR_STOP=1 "$@"; }
  psu -d postgres -c "SELECT 'server ' || current_setting('server_version') || ' port ' || current_setting('port')" \
    -c "SELECT 'role ' || rolname || ' super=' || rolsuper || ' createdb=' || rolcreatedb || ' login=' || rolcanlogin
        FROM pg_roles WHERE rolname !~ '^pg_' ORDER BY rolname" \
    -c "SELECT 'member ' || pg_get_userbyid(member) || ' of ' || pg_get_userbyid(roleid) FROM pg_auth_members ORDER BY 1" \
    -c "SELECT 'setting ' || name || '=' || setting FROM pg_settings
        WHERE source NOT IN ('default', 'override', 'client', 'session') ORDER BY name" \
    -c "SELECT 'acl ' || datname || ' owner=' || pg_get_userbyid(datdba) || ' ' || coalesce(datacl::text, 'default')
        FROM pg_database ORDER BY datname" || echo "superuser read failed"
  for db in $(psu -d postgres -c "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY 1"); do
    psu -d "$db" -c "SELECT 'db ' || current_database() || ' size=' || pg_database_size(current_database())
      || ' user_inserted=' || coalesce(sum(n_tup_ins), 0) || ' user_updated=' || coalesce(sum(n_tup_upd), 0)
      || ' user_deleted=' || coalesce(sum(n_tup_del), 0) FROM pg_stat_user_tables" || echo "db $db not readable"
  done
fi
if [ -n "${EXISTING_PG_URL:-}" ]; then
  section "existing PostgreSQL servers (read-only: roles, settings, database size and write counters)"
  .venv/bin/python scripts/homelab_bench.py pg-stats
fi
