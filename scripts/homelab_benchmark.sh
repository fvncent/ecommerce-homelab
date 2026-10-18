#!/usr/bin/env bash
# Homelab deployment verification with measurements: smoke (1k) and a publish crash (1k, own schemas, same database)
# -> MVP (10k, files) -> feed-only throughput -> SIGKILL during the load + resume -> replay killed + rerun -> cleanup
# of that replay (dry run; deletes only with CLEANUP_REPLAY=1).
# Works on Ubuntu (GNU time) and macOS (BSD time); bash 3.2 compatible.
#
# Databases (fixed in homelab_bench.py): ecommerce_homelab_smoke, ecommerce_homelab_test, ecommerce_homelab_crash.
# An operator creates them by hand, owned by a least-privilege role; preflight refuses otherwise or if any is not
# empty. This script never creates or drops a database (cleanup is a manual, approved step).
#
# Required env: ECOMGEN_BENCH_URL (or ECOMMERCE_DATABASE_URL) = URL of one of those databases as that role (local
#   only, explicit port, only sslmode/connect_timeout/application_name parameters, no PGHOST/PGHOSTADDR/PGSERVICE;
#   port 55432 is refused; must equal EXPECTED_PG_PORT, required on Linux); ECOMGEN_DATA_ROOT = where files are
#   published, an existing directory strictly inside DATA_ROOT (resolved, no symlink, not benchmarks/) on the mount.
#   On Linux DATA_ROOT is fixed to /srv/homelab/ecommerce.
# On Linux also: MOUNT_UUID = filesystem UUID that must be mounted (read-write) at MOUNT_TARGET (/srv/homelab) and
#   hold OUT; PG_SERVICE = systemd unit of the server (e.g. postgresql@18-main), proven to be the server the URL reaches.
# Optional env: DATA_ROOT (/srv/homelab/ecommerce), OUT (DATA_ROOT/benchmarks/<UTC>; must be under DATA_ROOT, its
#   parent must exist),
#   CONFIG (config/generator.example.yaml), EXPECTED_SHA (refuse unless HEAD is that commit/tag and the tree is
#   clean), EXPECTED_DIGEST (scripts/expected_mvp_digest.txt, the macOS baseline; must exist), PG_MAJOR (refuse
#   another server major version), PG_CONTAINER (docker container, instead of PG_SERVICE, for server memory/CPU on
#   macOS), PG_DATA_PATH (/var/lib/postgresql; free-space gate), MIN_FREE_GB (10), CLEANUP_REPLAY (1: really delete
#   the replay this run created), RECORD_REFERENCES (1: write the file references instead of comparing; macOS only,
#   refused with EXPECTED_SHA; exits 3).
# References (macOS, must exist unless recording): EXPECTED_DIGEST, scripts/expected_{smoke_feed,feed}_digest.txt, scripts/expected_replay_originals.txt.
# Exit 0 only if every check holds; 2 = aborted by a gate or a failed step; 3 = references recorded.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

export CONFIG="${CONFIG:-config/generator.example.yaml}"
export ECOMGEN_BENCH_URL="${ECOMGEN_BENCH_URL:-${ECOMMERCE_DATABASE_URL:-}}"
DATA_ROOT="${DATA_ROOT:-/srv/homelab/ecommerce}"
MOUNT_TARGET="${MOUNT_TARGET:-/srv/homelab}"
OUT="${OUT:-$DATA_ROOT/benchmarks/$(date -u +%Y%m%dT%H%M%SZ)}"
EXPECTED_DIGEST="${EXPECTED_DIGEST:-scripts/expected_mvp_digest.txt}"
REF_SMOKE_FEEDS=scripts/expected_smoke_feed_digest.txt
REF_FEEDS=scripts/expected_feed_digest.txt
REF_REPLAY=scripts/expected_replay_originals.txt
REPLAY_ARGS="2026-01-05 2026-01-10 24 6 10"  # from, to, delay h, jitter h, duplicate %
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

bench() { "$PY" scripts/homelab_bench.py "$@"; }  # a function: under env/timed call "$PY" scripts/... directly
early_die() { echo "ABORT: $*" >&2; exit 2; }  # before OUT exists
note() { echo "$*" | tee -a "$OUT/summary.md"; }
die() { note "ABORT: $*"; exit 2; }
# expect CODE LABEL CMD...: run CMD, record PASS/FAIL against the expected exit code
expect() {
  local want="$1" label="$2"; shift 2
  # a second command keeps the subshell from exec'ing "$@", so a SIGKILL notice comes from it (into the log)
  ( "$@" >> "$OUT/log.txt" 2>&1; exit $? ) 2>> "$OUT/log.txt"; local got=$?
  if [ "$got" = "$want" ]; then note "- PASS $label (exit $got)"; else note "- FAIL $label (exit $got, want $want)"; FAILED=1; fi
}
# same_as REF FILE LABEL: FILE must equal the macOS reference (or, when recording, becomes it); never empty
# (recording: the first, uninterrupted output of each reference is kept in OUT/references/ and later outputs are
# compared to it; the references are copied into scripts/ only at the end, and only if every check passed)
RECORDED=""
same_as() {
  if [ ! -s "$2" ]; then note "- FAIL $3 (no output)"; FAILED=1; return; fi
  if [ "${RECORD_REFERENCES:-}" = 1 ]; then
    local kept
    kept="$OUT/references/$(basename "$1")"
    if ! echo "$RECORDED" | grep -qw "$1"; then
      mkdir -p "$OUT/references"; cp "$2" "$kept"; RECORDED="$RECORDED $1"; note "- RECORDING $3 (pending: $kept)"
      return
    fi
    expect 0 "$3" diff "$kept" "$2"; return
  fi
  expect 0 "$3" diff "$1" "$2"
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
if [ "$(uname -s)" = Linux ]; then
  [ "$DATA_ROOT" = /srv/homelab/ecommerce ] || early_die "DATA_ROOT is fixed to /srv/homelab/ecommerce on Linux"
  [ -n "${EXPECTED_PG_PORT:-}" ] || early_die "EXPECTED_PG_PORT not set (5432 on the homelab)"
fi
[ -n "${ECOMGEN_DATA_ROOT:-}" ] || early_die "ECOMGEN_DATA_ROOT not set (e.g. $DATA_ROOT/data)"
[ -d "$ECOMGEN_DATA_ROOT" ] && [ ! -L "$ECOMGEN_DATA_ROOT" ] || early_die "$ECOMGEN_DATA_ROOT must be an existing directory, not a symlink"
REAL_PROJECT="$(realpath "$DATA_ROOT")" || early_die "cannot resolve $DATA_ROOT"
REAL_DATA="$(realpath "$ECOMGEN_DATA_ROOT")" || early_die "cannot resolve $ECOMGEN_DATA_ROOT"
case "$REAL_DATA" in "$REAL_PROJECT"/?*) ;; *) early_die "ECOMGEN_DATA_ROOT resolves to $REAL_DATA, not strictly inside $REAL_PROJECT";; esac
case "$REAL_DATA/" in "$REAL_PROJECT/benchmarks/"*) early_die "ECOMGEN_DATA_ROOT must not be inside $REAL_PROJECT/benchmarks";; esac
export ECOMGEN_DATA_ROOT="$REAL_DATA"  # resolved: no '..' or link anywhere in what the generator is given
REAL_PARENT="$(realpath "$PARENT" 2>/dev/null)" || early_die "$PARENT does not exist (create the project directories first)"
case "$REAL_PARENT/" in "$REAL_PROJECT"/?*) ;; *) early_die "OUT resolves outside $REAL_PROJECT";; esac
case "$REAL_PARENT/" in "$REAL_DATA/"*) early_die "OUT must not be inside the data root";; esac
if [ -n "${MOUNT_UUID:-}" ]; then
  [ "$(findmnt -no UUID -T "$ECOMGEN_DATA_ROOT" 2>/dev/null)" = "$MOUNT_UUID" ] || early_die "$ECOMGEN_DATA_ROOT is not on the $MOUNT_TARGET mount"
fi
if [ "${RECORD_REFERENCES:-}" = 1 ]; then
  [ -z "${EXPECTED_SHA:-}" ] && [ "$(uname -s)" = Darwin ] || early_die "RECORD_REFERENCES is for recording the macOS references only"
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
if [ "${RECORD_REFERENCES:-}" != 1 ]; then
  for ref in "$EXPECTED_DIGEST" "$REF_SMOKE_FEEDS" "$REF_FEEDS" "$REF_REPLAY"; do
    [ -s "$ref" ] || die "reference $ref missing"
  done
fi
lt="$(bench link-test 2>&1)" || die "hard-link test failed in $ECOMGEN_DATA_ROOT: $lt"
note "- data root: $lt"
fe="$(bench feeds-enabled 2>&1)" || die "$fe"
STARTED_EPOCH="$(bench server-epoch 2>> "$OUT/log.txt")" || die "cannot read the server clock"
note "- $fe"
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

# ---------- 1. smoke (1,000 orders), then a publish crash in its own schemas ----------
note ""; note "## Smoke (1,000 orders) in $SMOKE"
use_db "$SMOKE"
expect 0 "init-db" "$ECOMGEN" init-db "$CONFIG"
mem_start smoke
expect 0 "generate --profile smoke (database + files)" timed smoke "$ECOMGEN" generate "$CONFIG" --profile smoke
note "  - generator: $(time_summary smoke); postgres server: $(mem_stop smoke)"
expect 0 "validate (database + files)" "$ECOMGEN" validate "$CONFIG"
bench feed-digest "$SMOKE" > "$OUT/feeds_smoke.txt" 2>> "$OUT/log.txt"
same_as "$REF_SMOKE_FEEDS" "$OUT/feeds_smoke.txt" "smoke files identical to the macOS reference"
expect 0 "smoke files intact, no orphans or staging left" bench verify-files "$SMOKE"
bench digest "$SMOKE" > "$OUT/digest_smoke.txt" 2>> "$OUT/log.txt"
expect 0 "smoke digest complete (7 tables)" digest_ok "$OUT/digest_smoke.txt"

note ""; note "## Publish crash (1,000 orders, schemas *_pc in $SMOKE): SIGKILL after placing file 4, then resume"
PC_CONFIG="$OUT/generator_pc.yaml"
bench pc-config "$CONFIG" "$PC_CONFIG" >> "$OUT/log.txt" 2>&1 || die "cannot write $PC_CONFIG"
expect 0 "init-db (own, empty schema set: a clean baseline)" env CONFIG="$PC_CONFIG" "$ECOMGEN" init-db "$PC_CONFIG"
expect 137 "generate killed right after a file is in place, before its ledger row commits" env CONFIG="$PC_CONFIG" "$PY" scripts/homelab_bench.py publish-crash "$PC_CONFIG" 4
expect 0 "no sessions or prepared transactions left" env CONFIG="$PC_CONFIG" "$PY" scripts/homelab_bench.py leftovers "$SMOKE"
expect 1 "validate fails before resume" "$ECOMGEN" validate "$PC_CONFIG"
expect 0 "resume (adopts the placed file, publishes the rest)" timed pc_resume "$ECOMGEN" resume "$PC_CONFIG"
note "  - resume: $(time_summary pc_resume)"
expect 0 "validate after resume (database + files)" "$ECOMGEN" validate "$PC_CONFIG"
env CONFIG="$PC_CONFIG" "$PY" scripts/homelab_bench.py feed-digest "$SMOKE" > "$OUT/feeds_pc.txt" 2>> "$OUT/log.txt"
same_as "$REF_SMOKE_FEEDS" "$OUT/feeds_pc.txt" "files after crash + resume identical to the uninterrupted macOS reference"
expect 0 "ledger == disk: every recorded file intact, no unrecorded files, no staging left" env CONFIG="$PC_CONFIG" "$PY" scripts/homelab_bench.py verify-files "$SMOKE"
env CONFIG="$PC_CONFIG" "$PY" scripts/homelab_bench.py digest "$SMOKE" > "$OUT/digest_pc.txt" 2>> "$OUT/log.txt"
expect 0 "publish-crash digest complete (7 tables)" digest_ok "$OUT/digest_pc.txt"
expect 0 "database rows identical to the uninterrupted smoke run" diff "$OUT/digest_smoke.txt" "$OUT/digest_pc.txt"

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
same_as "$EXPECTED_DIGEST" "$OUT/digest_mvp.txt" "database rows identical to the macOS baseline"
bench feed-stats "$TEST" > "$OUT/feeds_stats_mvp.txt" 2>> "$OUT/log.txt"
while read -r line; do note "  - files: $line"; done < "$OUT/feeds_stats_mvp.txt"
bench feed-digest "$TEST" > "$OUT/feeds_mvp.txt" 2>> "$OUT/log.txt"
same_as "$REF_FEEDS" "$OUT/feeds_mvp.txt" "files identical to the macOS reference"
bench verify-files "$TEST" > "$OUT/verify_mvp.txt" 2>> "$OUT/log.txt"; v=$?
note "  - sha256 re-verification: $(head -1 "$OUT/verify_mvp.txt")"
expect 0 "every file matches its recorded sha256; no orphans" test "$v" = 0
note "  - end-to-end throughput (database + files): $(awk -v w="$(wall_of mvp)" '{r += $2; if ($1 == "orders") o = $2}
  END {if (w > 0) printf "%.0f orders/s, %.0f rows/s (%d rows)", o / w, r / w, r}' "$OUT/digest_mvp.txt")"

# ---------- 2b. feed-only throughput (no database) ----------
note ""; note "## Feed-only throughput (config counts, each feed alone, into $OUT/feedbench)"
mkdir "$OUT/feedbench"
for feed in payment_settlement clickstream; do
  expect 0 "$feed alone" timed "fb_$feed" "$PY" scripts/homelab_bench.py feed-throughput "$CONFIG" "$OUT/feedbench" "$feed"
  note "  - $(grep "^$feed " "$OUT/log.txt" | tail -1); $(time_summary "fb_$feed")"
done

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
bench feed-digest "$CRASH" > "$OUT/feeds_crash.txt" 2>> "$OUT/log.txt"
expect 0 "resumed run's files identical to the clean run's" diff "$OUT/feeds_mvp.txt" "$OUT/feeds_crash.txt"
expect 0 "resumed run's files intact, no orphans or staging left" bench verify-files "$CRASH"

# ---------- 4. replay: killed mid-way, rerun, verify ----------
note ""; note "## Replay of the MVP run's files ($REPLAY_ARGS): SIGKILL after delivery 6, rerun"
use_db "$TEST"
# shellcheck disable=SC2086  # REPLAY_ARGS is a fixed list of five words
set -- $REPLAY_ARGS
RPL_CLI=(--from "$1" --to "$2" --delay-hours "$3" --jitter-hours "$4" --duplicate-pct "$5")
expect 137 "replay killed right after a delivery is in place" bench replay-crash "$CONFIG" 6 "$@"
RID="$(bench replay-id "$TEST" "$@" 2>> "$OUT/log.txt")" || die "cannot compute the replay id"
expect 0 "rerun finishes the replay" timed replay "$ECOMGEN" replay-files "$CONFIG" "${RPL_CLI[@]}"
note "  - replay $RID:$(grep -h "^replay $RID" "$OUT/log.txt" | tail -1 | cut -d: -f2-); $(time_summary replay)"
"$ECOMGEN" replay-files "$CONFIG" "${RPL_CLI[@]}" > "$OUT/replay_again.txt" 2>&1; again=$?
expect 0 "second rerun succeeds" test "$again" = 0
expect 0 "second rerun only verifies" grep -q "verified (already complete)" "$OUT/replay_again.txt"
bench replay-originals "$TEST" "$@" > "$OUT/replay_originals.txt" 2>> "$OUT/log.txt"
same_as "$REF_REPLAY" "$OUT/replay_originals.txt" "replayed originals (paths, sha256) identical to the macOS reference"
# arrival times and duplicates are seeded by the replay id (includes the run id), so they are checked locally:
bench planned-replay-digest "$TEST" "$@" > "$OUT/replay_planned.txt" 2>> "$OUT/log.txt"
bench replay-digest "$TEST" "$RID" > "$OUT/replay_digest.txt" 2>> "$OUT/log.txt"
expect 0 "deliveries after kill + rerun are exactly the plan (none missing, none extra)" diff "$OUT/replay_planned.txt" "$OUT/replay_digest.txt"
expect 0 "deliveries intact, no unrecorded files in the replay directory, no staging left" bench verify-replay "$TEST" "$RID"

# ---------- 5. cleanup of that replay (dry run unless CLEANUP_REPLAY=1) ----------
note ""; note "## Cleanup of replay $RID"
RUN_MVP="$(bench run-id "$TEST" 2>> "$OUT/log.txt")"
expect 0 "the replay was created by this benchmark (run $RUN_MVP, after $STARTED_EPOCH)" bench owned-replay "$TEST" "$RID" "$RUN_MVP" "$STARTED_EPOCH"
"$ECOMGEN" cleanup-files "$CONFIG" --replay-id "$RID" > "$OUT/cleanup_plan.txt" 2>&1; dry=$?
expect 0 "cleanup dry run succeeds" test "$dry" = 0
note "  - $(head -1 "$OUT/cleanup_plan.txt") (full list: cleanup_plan.txt)"
expect 0 "dry run lists this replay's copies" grep -q "^  delete deliveries/replay_id=$RID/arrived=" "$OUT/cleanup_plan.txt"
expect 0 "dry run lists nothing else" sh -c "! grep '^  delete ' '$OUT/cleanup_plan.txt' | grep -v '^  delete deliveries/replay_id=$RID/arrived='"
TOKEN="$(sed -n 's/.*--delete --confirm \([0-9a-f]\{12\}\)$/\1/p' "$OUT/cleanup_plan.txt")"
if [ "${CLEANUP_REPLAY:-}" = 1 ] && [ -n "$TOKEN" ] && [ "$FAILED" = 0 ]; then
  expect 0 "cleanup --delete --confirm (CLEANUP_REPLAY=1)" "$ECOMGEN" cleanup-files "$CONFIG" --replay-id "$RID" --delete --confirm "$TOKEN"
  note "  - $(grep -h "^cleanup cln_" "$OUT/log.txt" | tail -1)"
  expect 0 "originals untouched (every file still matches its sha256)" bench verify-files "$TEST"
  expect 0 "MVP run still validates" "$ECOMGEN" validate "$CONFIG"
  expect 1 "the cleaned replay can no longer be verified (and says why)" "$ECOMGEN" replay-files "$CONFIG" "${RPL_CLI[@]}"
elif [ "${CLEANUP_REPLAY:-}" = 1 ]; then
  note "- FAIL CLEANUP_REPLAY=1 but nothing was deleted (earlier failures, or no token in the dry run)"; FAILED=1
else
  note "  - not deleted (dry run only; set CLEANUP_REPLAY=1 to delete, only after all other checks pass)"
fi

# ---------- totals ----------
note ""; note "## Totals"
for db in "$SMOKE" "$TEST" "$CRASH"; do
  note "- $(bench sizes "$db" 2>> "$OUT/log.txt" | awk 'NR == 1 {printf "%s %.1f MB", $2, $3 / 1048576}')"
done
note "- data root: $(du -sk "$ECOMGEN_DATA_ROOT" | awk '{printf "%.1f MB", $1 / 1024}') in $ECOMGEN_DATA_ROOT"
note "- disk after: $(df -Ph "$OUT" | awk 'NR == 2 {print $3 " used, " $4 " free on " $6}')"
note "- nothing dropped; all three databases kept (cleanup is a manual, approved step: docs/HOMELAB_DEPLOYMENT.md)"
if [ "${RECORD_REFERENCES:-}" = 1 ]; then
  if [ "$FAILED" != 0 ]; then note "- RESULT: FAILURES above; references NOT recorded"; exit 2; fi
  for f in $RECORDED; do cp "$OUT/references/$(basename "$f")" "$f"; done
  note "- RESULT: references recorded into scripts/ (not a verification run)"; exit 3
fi
if [ "$FAILED" = 0 ]; then note "- RESULT: all expectations passed"; else note "- RESULT: FAILURES above (details in log.txt)"; fi
exit "$FAILED"
