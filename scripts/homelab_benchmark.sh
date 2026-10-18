#!/usr/bin/env bash
# Homelab deployment verification: smoke (1k) -> MVP benchmark (10k) -> SIGKILL + resume, with measurements.
# Works on Ubuntu (GNU time) and macOS (BSD time); bash 3.2 compatible.
#
# Databases (fixed in homelab_bench.py): ecommerce_homelab_smoke, ecommerce_homelab_test, ecommerce_homelab_crash.
# An operator creates them by hand, owned by a least-privilege role; preflight refuses otherwise or if any is not
# empty. This script never creates or drops a database (cleanup is a manual, approved step).
#
# Required env: ECOMGEN_BENCH_URL (or ECOMMERCE_DATABASE_URL) = URL of one of those databases as that role.
# On Linux also: MOUNT_UUID = filesystem UUID that must be mounted (read-write) at MOUNT_TARGET (/srv/homelab) and
#   hold OUT; PG_SERVICE = systemd unit of the server (e.g. postgresql@18-main), proven to be the server the URL reaches.
# Optional env: DATA_ROOT (/srv/homelab/ecommerce), OUT (DATA_ROOT/benchmarks/<UTC>; must be under DATA_ROOT, its
#   parent must exist),
#   CONFIG (config/generator.example.yaml), EXPECTED_SHA (refuse unless HEAD is that commit/tag and the tree is
#   clean), EXPECTED_DIGEST (scripts/expected_mvp_digest.txt, the macOS baseline; must exist), PG_MAJOR (refuse
#   another server major version), PG_CONTAINER (docker container, instead of PG_SERVICE, for server memory/CPU on
#   macOS), PG_DATA_PATH (/var/lib/postgresql; free-space gate), MIN_FREE_GB (10).
# Exit 0 only if every check holds; 2 = aborted by a gate or a failed step.
set -uo pipefail
cd "$(dirname "$0")/.."

export CONFIG="${CONFIG:-config/generator.example.yaml}"
export ECOMGEN_BENCH_URL="${ECOMGEN_BENCH_URL:-${ECOMMERCE_DATABASE_URL:-}}"
DATA_ROOT="${DATA_ROOT:-/srv/homelab/ecommerce}"
MOUNT_TARGET="${MOUNT_TARGET:-/srv/homelab}"
OUT="${OUT:-$DATA_ROOT/benchmarks/$(date -u +%Y%m%dT%H%M%SZ)}"
EXPECTED_DIGEST="${EXPECTED_DIGEST:-scripts/expected_mvp_digest.txt}"
PG_DATA_PATH="${PG_DATA_PATH:-/var/lib/postgresql}"
MIN_FREE_GB="${MIN_FREE_GB:-10}"
SMOKE=ecommerce_homelab_smoke
TEST=ecommerce_homelab_test
CRASH=ecommerce_homelab_crash
PY=.venv/bin/python
ECOMGEN=.venv/bin/ecomgen
FAILED=0
MEMPID=""
PG_CGROUP=""

bench() { "$PY" scripts/homelab_bench.py "$@"; }
early_die() { echo "ABORT: $*" >&2; exit 2; }  # before OUT exists
note() { echo "$*" | tee -a "$OUT/summary.md"; }
die() { note "ABORT: $*"; exit 2; }
# expect CODE LABEL CMD...: run CMD, record PASS/FAIL against the expected exit code
expect() {
  local want="$1" label="$2"; shift 2
  "$@" >> "$OUT/log.txt" 2>&1; local got=$?
  if [ "$got" = "$want" ]; then note "- PASS $label (exit $got)"; else note "- FAIL $label (exit $got, want $want)"; FAILED=1; fi
}
# digest_ok FILE: the digest step produced all 7 tables (an empty pair must never compare "identical")
digest_ok() { [ "$(wc -l < "$1" | tr -d ' ')" = 7 ]; }
timed() {
  local name="$1"; shift
  if [ "$TIME_STYLE" = gnu ]; then /usr/bin/time -v "$@" 2> "$OUT/time_$name.txt"
  else /usr/bin/time -l "$@" 2> "$OUT/time_$name.txt"; fi
}
time_summary() {  # NAME -> "wall=Ns user=Ns sys=Ns max_rss=NMB"
  awk '
    /Elapsed \(wall clock\)/ { n = split($NF, p, ":"); wall = (n == 3) ? p[1]*3600 + p[2]*60 + p[3] : p[1]*60 + p[2] }
    /User time \(seconds\)/ { user = $NF } /System time \(seconds\)/ { sys = $NF }
    /Maximum resident set size \(kbytes\)/ { rss = $NF / 1024 }
    / real .* user .* sys/ { wall = $1; user = $3; sys = $5 }
    /maximum resident set size/ && !/kbytes/ { rss = $1 / 1048576 }
    END { printf "wall=%.2fs user=%.2fs sys=%.2fs max_rss=%.0fMB", wall, user, sys, rss }' "$OUT/time_$1.txt"
}
wall_of() { time_summary "$1" | sed 's/^wall=\([0-9.]*\)s.*/\1/'; }
# PostgreSQL server cgroup (systemd unit or docker container): memory includes page cache; CPU is all its processes.
pgcg() {
  if [ -n "$PG_CGROUP" ]; then cat "$PG_CGROUP/$1" 2>/dev/null
  elif [ -n "${PG_CONTAINER:-}" ]; then docker exec "$PG_CONTAINER" cat "/sys/fs/cgroup/$1" 2>/dev/null; fi
}
cpu_usec() { pgcg cpu.stat | awk '/^usage_usec/ {print $2}'; }
cpu_since() { awk -v a="${1:-0}" -v b="$(cpu_usec)" 'BEGIN {print (a > 0 && b > 0) ? sprintf("%.2fs", (b - a) / 1e6) : "n/a"}'; }
mem_start() {  # sample memory.current every 0.2 s for one phase
  ( while :; do pgcg memory.current; sleep 0.2; done ) > "$OUT/mem_$1.txt" 2>/dev/null &
  MEMPID=$!; disown "$MEMPID" 2>/dev/null
}
mem_stop() {
  kill "$MEMPID" 2>/dev/null; MEMPID=""
  awk '$1 + 0 > m { m = $1 + 0 } END { if (NR) printf "%.0f MB peak (%d samples)", m / 1048576, NR; else print "not measured" }' "$OUT/mem_$1.txt"
}
cleanup() { [ -n "$MEMPID" ] && kill "$MEMPID" 2>/dev/null; }
use_db() { ECOMMERCE_DATABASE_URL="$(bench url "$1")" || die "cannot build URL for $1"; export ECOMMERCE_DATABASE_URL; }
free_gb() { df -Pk "$1" 2>/dev/null | awk 'NR == 2 {print int($4 / 1048576)}'; }

# ---------- gates before anything is written ----------
case "$OUT" in "$DATA_ROOT"/*) ;; *) early_die "OUT $OUT is not under DATA_ROOT $DATA_ROOT";; esac
PARENT="$(dirname "$OUT")"
if [ -n "${MOUNT_UUID:-}" ]; then
  command -v findmnt >/dev/null || early_die "findmnt not found"
  mnt() { findmnt -no "$1" -T "$PARENT" 2>/dev/null; }  # one field per call: an empty UUID can't shift fields
  m_target="$(mnt TARGET)"; m_uuid="$(mnt UUID)"; m_opts="$(mnt OPTIONS)"
  [ "${m_target:-}" = "$MOUNT_TARGET" ] || early_die "$PARENT is not on the $MOUNT_TARGET mount (found ${m_target:-nothing}); is the NVMe mounted?"
  [ "${m_uuid:-}" = "$MOUNT_UUID" ] || early_die "$MOUNT_TARGET is filesystem ${m_uuid:-?}, expected $MOUNT_UUID"
  case ",${m_opts:-}," in *,rw,*) ;; *) early_die "$MOUNT_TARGET is not mounted read-write (${m_opts:-})";; esac
elif [ "$(uname -s)" = Linux ]; then
  early_die "MOUNT_UUID not set (the filesystem UUID expected at $MOUNT_TARGET)"
fi
[ "$(uname -s)" != Linux ] || [ -n "${PG_SERVICE:-}" ] || early_die "PG_SERVICE not set (e.g. postgresql@18-main)"
[ -d "$PARENT" ] || early_die "$PARENT does not exist (create the project directories first)"
mkdir "$OUT" || early_die "cannot create $OUT"
: > "$OUT/summary.md"; : > "$OUT/log.txt"
trap cleanup EXIT

# ---------- preflight gates ----------
note "# Homelab benchmark $(date -u +%Y-%m-%dT%H:%M:%SZ)"
note "- host: $(uname -srm); commit: $(git rev-parse --short HEAD); config: $CONFIG; out: $OUT"
[ -z "${MOUNT_UUID:-}" ] || note "- mount: $MOUNT_TARGET = UUID $MOUNT_UUID, read-write"
[ -n "$ECOMGEN_BENCH_URL" ] || die "ECOMGEN_BENCH_URL / ECOMMERCE_DATABASE_URL not set"
if [ -n "${EXPECTED_SHA:-}" ]; then
  want="$(git rev-parse --verify --quiet "${EXPECTED_SHA}^{commit}")" || die "unknown commit/tag $EXPECTED_SHA"
  [ "$(git rev-parse HEAD)" = "$want" ] || die "HEAD is not the approved commit $EXPECTED_SHA"
  [ -z "$(git status --porcelain)" ] || die "working tree is not clean"
fi
[ -f "$EXPECTED_DIGEST" ] || die "baseline $EXPECTED_DIGEST missing"
if /usr/bin/time -v true >/dev/null 2>&1; then TIME_STYLE=gnu
elif [ "$(uname -s)" = Darwin ] && /usr/bin/time -l true >/dev/null 2>&1; then TIME_STYLE=bsd
else die "GNU time not found (Ubuntu: sudo apt install time)"; fi
pre="$(bench preflight 2>&1)" || die "$pre"
note "- $pre"
if [ -n "${PG_MAJOR:-}" ]; then
  major="$(bench server-major 2>>"$OUT/log.txt")" || die "cannot read server version"
  [ "$major" = "$PG_MAJOR" ] || die "server is PostgreSQL $major, expected $PG_MAJOR"
fi
if [ -n "${PG_SERVICE:-}" ]; then
  cg="$(systemctl show -p ControlGroup --value "$PG_SERVICE" 2>/dev/null)"
  PG_CGROUP="/sys/fs/cgroup$cg"
  [ -n "$cg" ] && [ -r "$PG_CGROUP/memory.current" ] || die "cannot read memory.current of $PG_SERVICE ($PG_CGROUP)"
  same="$(bench same-server cgroup "$PG_CGROUP" 2>&1)" || die "the URL's server is not $PG_SERVICE: $same"
  note "- server metrics: systemd unit $PG_SERVICE ($PG_CGROUP); $same"
elif [ -n "${PG_CONTAINER:-}" ]; then
  same="$(bench same-server container "$PG_CONTAINER" 2>&1)" || die "the URL's server is not container $PG_CONTAINER: $same"
  note "- server metrics: container $PG_CONTAINER; $same"
else
  note "- server metrics: not measured (set PG_SERVICE or PG_CONTAINER)"
fi
[ "$(free_gb "$OUT")" -ge "$MIN_FREE_GB" ] || die "less than ${MIN_FREE_GB} GB free on $OUT"
if [ -d "$PG_DATA_PATH" ]; then
  [ "$(free_gb "$PG_DATA_PATH")" -ge "$MIN_FREE_GB" ] || die "less than ${MIN_FREE_GB} GB free on $PG_DATA_PATH"
else
  note "- note: $PG_DATA_PATH not on this host; its free-space gate skipped"
fi
note "- disk before: $(df -Ph "$OUT" | awk 'NR == 2 {print $3 " used, " $4 " free on " $6}')"

# ---------- 1. smoke (1,000 orders) ----------
note ""; note "## Smoke (1,000 orders) in $SMOKE"
use_db "$SMOKE"
expect 0 "init-db" "$ECOMGEN" init-db "$CONFIG"
mem_start smoke
expect 0 "generate --profile smoke" timed smoke "$ECOMGEN" generate "$CONFIG" --profile smoke
note "  - generator: $(time_summary smoke); postgres server: $(mem_stop smoke)"
expect 0 "validate" "$ECOMGEN" validate "$CONFIG"

# ---------- 2. MVP benchmark (10,000 orders) ----------
note ""; note "## MVP (10,000 orders, config counts) in $TEST"
use_db "$TEST"
expect 0 "init-db" "$ECOMGEN" init-db "$CONFIG"
lsn="$(bench wal-lsn)" || die "cannot read WAL position"; cpu0="$(cpu_usec)"
mem_start mvp
expect 0 "generate" timed mvp "$ECOMGEN" generate "$CONFIG"
mem="$(mem_stop mvp)"
wal="$(bench wal-bytes-since "$lsn")" || die "cannot read WAL position"
note "  - generator: $(time_summary mvp)"
note "  - postgres server: $mem; CPU $(cpu_since "$cpu0")"
note "  - WAL during generate (server-wide): $(awk -v b="$wal" 'BEGIN {printf "%.1f MB", b / 1048576}')"
bench sizes "$TEST" > "$OUT/sizes_mvp.txt" 2>> "$OUT/log.txt" || die "cannot read sizes"
note "  - $(awk 'NR == 1 {printf "database size %.1f MB", $3 / 1048576}' "$OUT/sizes_mvp.txt")"
expect 0 "validate" timed validate "$ECOMGEN" validate "$CONFIG"
note "  - validate: $(time_summary validate)"
"$ECOMGEN" status "$CONFIG" >> "$OUT/log.txt" 2>&1
bench digest "$TEST" > "$OUT/digest_mvp.txt" 2>> "$OUT/log.txt"
expect 0 "digest complete (7 tables)" digest_ok "$OUT/digest_mvp.txt"
expect 0 "data identical to macOS baseline" diff "$EXPECTED_DIGEST" "$OUT/digest_mvp.txt"
note "  - throughput: $(awk -v w="$(wall_of mvp)" '{r += $2; if ($1 == "orders") o = $2}
  END {if (w > 0) printf "%.0f orders/s, %.0f rows/s (%d rows)", o / w, r / w, r}' "$OUT/digest_mvp.txt")"

# ---------- 3. interrupted generation + resume ----------
note ""; note "## SIGKILL before orders chunk 1, then resume, in $CRASH"
use_db "$CRASH"
expect 0 "init-db" "$ECOMGEN" init-db "$CONFIG"
expect 137 "generate killed by SIGKILL" bench crash "$CONFIG" orders 1
expect 0 "no sessions or prepared transactions left by the killed run" bench leftovers "$CRASH"
"$ECOMGEN" status "$CONFIG" > "$OUT/status_crashed.txt" 2>&1
expect 0 "status reports the crash" grep -q "the run crashed" "$OUT/status_crashed.txt"
expect 1 "validate fails on the partial run" "$ECOMGEN" validate "$CONFIG"
expect 1 "generate refuses (run exists)" "$ECOMGEN" generate "$CONFIG"
cpu0="$(cpu_usec)"
mem_start resume
expect 0 "resume" timed resume "$ECOMGEN" resume "$CONFIG"
note "  - resume: $(time_summary resume); postgres server: $(mem_stop resume); CPU $(cpu_since "$cpu0")"
expect 0 "validate after resume" "$ECOMGEN" validate "$CONFIG"
expect 0 "second resume is a no-op" "$ECOMGEN" resume "$CONFIG"
bench digest "$CRASH" > "$OUT/digest_crash.txt" 2>> "$OUT/log.txt"
expect 0 "digest complete (7 tables)" digest_ok "$OUT/digest_crash.txt"
expect 0 "resumed data identical to the clean run (no duplicates, nothing missing)" diff "$OUT/digest_mvp.txt" "$OUT/digest_crash.txt"

# ---------- totals ----------
note ""; note "## Totals"
for db in "$SMOKE" "$TEST" "$CRASH"; do
  note "- $(bench sizes "$db" 2>> "$OUT/log.txt" | awk 'NR == 1 {printf "%s %.1f MB", $2, $3 / 1048576}')"
done
note "- disk after: $(df -Ph "$OUT" | awk 'NR == 2 {print $3 " used, " $4 " free on " $6}')"
note "- nothing dropped; all three databases kept (cleanup is a manual, approved step: docs/HOMELAB_DEPLOYMENT.md)"
if [ "$FAILED" = 0 ]; then note "- RESULT: all expectations passed"; else note "- RESULT: FAILURES above (details in log.txt)"; fi
exit "$FAILED"
