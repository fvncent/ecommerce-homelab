#!/usr/bin/env bash
# Homelab deployment verification: smoke (1k) -> MVP benchmark (10k) -> SIGKILL + resume, with measurements.
# Works on Ubuntu (GNU time) and macOS (BSD time); bash 3.2 compatible.
#
# Required env: ECOMGEN_TEST_DATABASE_URL = admin URL of OUR compose PostgreSQL's maintenance DB (.../postgres);
#   preflight proves it reaches the compose container (same cluster system_identifier).
# Optional env: CONFIG (config/generator.example.yaml), OUT (/srv/ecommerce-data/benchmarks/<UTC>),
#   EXPECTED_SHA (refuse unless HEAD is that commit/tag and the tree is clean), EXPECTED_DIGEST
#   (scripts/expected_mvp_digest.txt, the macOS baseline; must exist), MIN_FREE_GB (10).
#
# Databases (namespace fixed in homelab_bench.py): ecommerce_homelab_test (kept), ecommerce_homelab_test_smoke and
# ecommerce_homelab_test_crash (dropped, also on abort). Refuses if any exists. Exit 0 only if every check holds.
set -uo pipefail
cd "$(dirname "$0")/.."

export CONFIG="${CONFIG:-config/generator.example.yaml}"
OUT="${OUT:-/srv/ecommerce-data/benchmarks/$(date -u +%Y%m%dT%H%M%SZ)}"
EXPECTED_DIGEST="${EXPECTED_DIGEST:-scripts/expected_mvp_digest.txt}"
MIN_FREE_GB="${MIN_FREE_GB:-10}"
BASE=ecommerce_homelab_test
SMOKE="${BASE}_smoke"
CRASH="${BASE}_crash"
PY=.venv/bin/python
ECOMGEN=.venv/bin/ecomgen
FAILED=0
CREATED_SIBLINGS=""
MEMPID=""

bench() { "$PY" scripts/homelab_bench.py "$@"; }
note() { echo "$*" | tee -a "$OUT/summary.md"; }
die() { note "ABORT: $*"; exit 2; }
# must CMD...: a step the run can't continue without
must() { "$@" >> "$OUT/log.txt" 2>&1 || die "step failed: $* (see log.txt)"; }
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
cgroup() { docker exec "$CONTAINER" cat "/sys/fs/cgroup/$1" 2>/dev/null; }
cpu_usec() { cgroup cpu.stat | awk '/^usage_usec/ {print $2}'; }
# Per-phase container memory: sample cgroup memory.current (includes page cache) every 0.2 s.
mem_start() {
  ( while :; do cgroup memory.current; sleep 0.2; done ) > "$OUT/mem_$1.txt" 2>/dev/null &
  MEMPID=$!; disown "$MEMPID" 2>/dev/null
}
mem_stop() {
  kill "$MEMPID" 2>/dev/null; MEMPID=""
  awk '$1 + 0 > m { m = $1 + 0 } END { printf "%.0f MB peak (%d samples)", m / 1048576, NR }' "$OUT/mem_$1.txt"
}
cleanup() {
  [ -n "$MEMPID" ] && kill "$MEMPID" 2>/dev/null
  for db in $CREATED_SIBLINGS; do bench drop "$db" >> "$OUT/log.txt" 2>&1 || echo "WARNING: could not drop $db" >&2; done
}
create_db() {  # DB: create and remember siblings for cleanup
  must bench create "$1"
  [ "$1" = "$BASE" ] || CREATED_SIBLINGS="$CREATED_SIBLINGS $1"
}
drop_sibling() {
  must bench drop "$1"
  CREATED_SIBLINGS="$(echo "$CREATED_SIBLINGS" | tr ' ' '\n' | grep -vx "$1" | tr '\n' ' ')"
}
use_db() { ECOMMERCE_DATABASE_URL="$(bench url "$1")" || die "cannot build URL for $1"; export ECOMMERCE_DATABASE_URL; }

# ---------- preflight gates ----------
mkdir -p "$OUT" || { echo "ABORT: cannot create $OUT" >&2; exit 2; }
: > "$OUT/summary.md"; : > "$OUT/log.txt"
trap cleanup EXIT
note "# Homelab benchmark $(date -u +%Y-%m-%dT%H:%M:%SZ)"
note "- host: $(uname -srm); commit: $(git rev-parse --short HEAD); config: $CONFIG; out: $OUT"
[ -n "${ECOMGEN_TEST_DATABASE_URL:-}" ] || die "ECOMGEN_TEST_DATABASE_URL not set"
if [ -n "${EXPECTED_SHA:-}" ]; then
  want="$(git rev-parse --verify --quiet "${EXPECTED_SHA}^{commit}")" || die "unknown commit/tag $EXPECTED_SHA"
  [ "$(git rev-parse HEAD)" = "$want" ] || die "HEAD is not the approved commit $EXPECTED_SHA"
  [ -z "$(git status --porcelain)" ] || die "working tree is not clean"
fi
[ -f "$EXPECTED_DIGEST" ] || die "baseline $EXPECTED_DIGEST missing"
if /usr/bin/time -v true >/dev/null 2>&1; then TIME_STYLE=gnu
elif [ "$(uname -s)" = Darwin ] && /usr/bin/time -l true >/dev/null 2>&1; then TIME_STYLE=bsd
else die "GNU time not found (Ubuntu: sudo apt install time)"; fi
CONTAINER="$(docker compose ps -q postgres 2>/dev/null)"
[ -n "$CONTAINER" ] || die "compose postgres container not running"
inside="$(docker exec "$CONTAINER" psql -U ecomgen -d postgres -tAc 'SELECT system_identifier FROM pg_control_system()' 2>/dev/null)"
url_side="$(bench system-id 2>>"$OUT/log.txt")" || die "ECOMGEN_TEST_DATABASE_URL refused or unreachable (see log.txt)"
[ -n "$inside" ] && [ "$inside" = "$url_side" ] || die "ECOMGEN_TEST_DATABASE_URL does not reach the compose container"
for db in "$BASE" "$SMOKE" "$CRASH"; do
  bench url "$db" > /dev/null || die "database name $db refused"
  if bench exists "$db"; then die "database $db already exists (never reused)"; fi
done
free_gb() { df -Pk "$1" 2>/dev/null | awk 'NR == 2 {print int($4 / 1048576)}'; }
[ "$(free_gb "$OUT")" -ge "$MIN_FREE_GB" ] || die "less than ${MIN_FREE_GB} GB free on $OUT"
docker_root="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null)"
if [ -d "$docker_root" ]; then
  [ "$(free_gb "$docker_root")" -ge "$MIN_FREE_GB" ] || die "less than ${MIN_FREE_GB} GB free on Docker root $docker_root"
else
  note "- note: Docker root $docker_root not visible on this host (Docker Desktop VM); free-space gate skipped for it"
fi
note "- postgres: $(docker exec "$CONTAINER" psql -U ecomgen -d postgres -tAc 'SHOW server_version'); container $(docker inspect -f '{{.Name}} {{.Config.Image}}' "$CONTAINER"); verified same cluster as ECOMGEN_TEST_DATABASE_URL"
note "- disk before: $(df -Ph "$OUT" | awk 'NR == 2 {print $3 " used, " $4 " free on " $6}')"

# ---------- 1. smoke (1,000 orders) ----------
note ""; note "## Smoke (1,000 orders)"
create_db "$SMOKE"; use_db "$SMOKE"
expect 0 "init-db" "$ECOMGEN" init-db "$CONFIG"
mem_start smoke
expect 0 "generate --profile smoke" timed smoke "$ECOMGEN" generate "$CONFIG" --profile smoke
note "  - generator: $(time_summary smoke); postgres container: $(mem_stop smoke)"
expect 0 "validate" "$ECOMGEN" validate "$CONFIG"
drop_sibling "$SMOKE"

# ---------- 2. MVP benchmark (10,000 orders) ----------
note ""; note "## MVP (10,000 orders, config counts)"
create_db "$BASE"; use_db "$BASE"
expect 0 "init-db" "$ECOMGEN" init-db "$CONFIG"
lsn="$(bench wal-lsn)" || die "cannot read WAL position"; cpu0="$(cpu_usec)"
mem_start mvp
expect 0 "generate" timed mvp "$ECOMGEN" generate "$CONFIG"
mem="$(mem_stop mvp)"
wal="$(bench wal-bytes-since "$lsn")" || die "cannot read WAL position"; cpu1="$(cpu_usec)"
note "  - generator: $(time_summary mvp)"
note "  - postgres container: $mem; CPU $(awk -v a="${cpu0:-0}" -v b="${cpu1:-0}" 'BEGIN {print (a > 0 && b > 0) ? sprintf("%.2fs", (b - a) / 1e6) : "n/a"}')"
note "  - WAL during generate: $(awk -v b="$wal" 'BEGIN {printf "%.1f MB", b / 1048576}')"
bench sizes "$BASE" > "$OUT/sizes_mvp.txt" 2>> "$OUT/log.txt" || die "cannot read sizes"
note "  - $(awk 'NR == 1 {printf "database size %.1f MB", $3 / 1048576}' "$OUT/sizes_mvp.txt")"
expect 0 "validate" timed validate "$ECOMGEN" validate "$CONFIG"
note "  - validate: $(time_summary validate)"
"$ECOMGEN" status "$CONFIG" >> "$OUT/log.txt" 2>&1
bench digest "$BASE" > "$OUT/digest_mvp.txt" 2>> "$OUT/log.txt"
expect 0 "digest complete (7 tables)" digest_ok "$OUT/digest_mvp.txt"
expect 0 "data identical to macOS baseline" diff "$EXPECTED_DIGEST" "$OUT/digest_mvp.txt"

# ---------- 3. interrupted generation + resume ----------
note ""; note "## SIGKILL before orders chunk 1, then resume"
create_db "$CRASH"; use_db "$CRASH"
expect 0 "init-db" "$ECOMGEN" init-db "$CONFIG"
expect 137 "generate killed by SIGKILL" bench crash "$CONFIG" orders 1
"$ECOMGEN" status "$CONFIG" > "$OUT/status_crashed.txt" 2>&1
expect 0 "status reports the crash" grep -q "the run crashed" "$OUT/status_crashed.txt"
expect 1 "validate fails on the partial run" "$ECOMGEN" validate "$CONFIG"
expect 1 "generate refuses (run exists)" "$ECOMGEN" generate "$CONFIG"
mem_start resume
expect 0 "resume" timed resume "$ECOMGEN" resume "$CONFIG"
note "  - resume: $(time_summary resume); postgres container: $(mem_stop resume)"
expect 0 "validate after resume" "$ECOMGEN" validate "$CONFIG"
expect 0 "second resume is a no-op" "$ECOMGEN" resume "$CONFIG"
bench digest "$CRASH" > "$OUT/digest_crash.txt" 2>> "$OUT/log.txt"
expect 0 "digest complete (7 tables)" digest_ok "$OUT/digest_crash.txt"
expect 0 "resumed data identical to the clean run" diff "$OUT/digest_mvp.txt" "$OUT/digest_crash.txt"
drop_sibling "$CRASH"

# ---------- totals ----------
note ""; note "## Totals"
peak="$(cgroup memory.peak)"
note "- postgres container memory.peak since container start (includes anything run before, e.g. pytest): $(awk -v b="${peak:-0}" 'BEGIN {print (b > 0) ? sprintf("%.0f MB", b / 1048576) : "n/a (kernel < 5.19)"}')"
note "- disk after: $(df -Ph "$OUT" | awk 'NR == 2 {print $3 " used, " $4 " free on " $6}')"
note "- kept: $BASE; dropped: $SMOKE, $CRASH"
if [ "$FAILED" = 0 ]; then note "- RESULT: all expectations passed"; else note "- RESULT: FAILURES above (details in log.txt)"; fi
exit "$FAILED"
