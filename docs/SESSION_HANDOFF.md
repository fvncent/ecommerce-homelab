# Session Handoff

Update before compaction or ending a significant session. Overwrite; git history keeps the old ones. Task list: `TASKS.md`.

**Updated:** 2026-10-18
**Branch / commit:** `main` @ `05bccd0`, tagged **`v0.1.0-homelab`** (annotated) — the approved deploy target.
PRs #5-#10 merged into `main` on 2026-10-18 (user-approved option (a)): merge commits, each pinned to its reviewed head
(`--match-head-commit`), each retargeted to `main` after its parent merged. `main`'s tree is identical to the tested #10
head `89d530b`; on `main`: 139 passed / 0 skipped, mypy clean. Old stacked branches remain on GitHub (not deleted).
Open decisions from the plan: sibling smoke/crash DBs (defaults used), where Ubuntu's existing `ecommerce_oltp` lives (for `EXISTING_PG_URL`), who executes on Ubuntu (user vs approved SSH), GitHub access (deploy key recommended).

## Current phase and task
Ubuntu Homelab Deployment Verification — tooling PR (no Ubuntu access yet; nothing run there).
- `compose.yaml`: `POSTGRES_DB` configurable (default `ecommerce_oltp`, Mac unchanged), healthcheck uses it; homelab sets `POSTGRES_DB=postgres` + `COMPOSE_PROJECT_NAME=ecomgen_homelab`. Verified on a throwaway project (port 5499): healthy, no extra DB, removed.
- `scripts/homelab_bench.py`: hard-coded namespace (create `ecommerce_homelab_test[_*]`, drop only `ecommerce_homelab_test_*`), refuses existing DBs and URLs hiding their DB (`?dbname=`, `%`), URLs from env only, read-only `pg-stats` (user-table counters: catalog counters move with autoanalyze), SIGKILL crash runner (sibling targets only). 16 unit tests, no DB.
- `scripts/homelab_benchmark.sh`: gates (approved commit via `^{commit}` + clean tree, baseline present, GNU time, container up, **admin URL reaches our container** by `system_identifier`, DBs absent, ≥10 GB free on output + Docker root) → smoke → 10k (time, per-phase container memory sampled 0.2 s, cgroup CPU, WAL, sizes, validate, 7-table digest vs macOS) → SIGKILL + resume (digest = clean) → totals. Critical steps abort; siblings dropped on exit. Exit 0 only if all pass.
- `scripts/host_snapshot.sh`: read-only before/after (containers, volumes, networks, images, compose projects, ports, /srv manifest excluding our checkout + benchmark output, optional existing-PG user-table counters).
- `scripts/expected_mvp_digest.txt`: macOS 10k reference (matches earlier run). `docs/HOMELAB_DEPLOYMENT.md`: runbook. mypy now covers `scripts/`.
- Mac dry run after fixes: all expectations passed; snapshot diff = only the new benchmark DB; dev `ecommerce_oltp` identical. Abort paths tested: admin URL to another server (throwaway PG on 5499), dirty tree with EXPECTED_SHA, `?dbname=` URL — all abort before creating anything. Kept DB dropped afterwards via psql.
- Tests: 139 passed / 0 failed / 0 skipped with DB (first run after a benchmark takes ~60 s while Docker Desktop settles; re-run 20 s); 66 passed / 73 skipped without DB; mypy clean (18 files incl. scripts/).

## Review (`/code-review high`) — all 10 fixed
| # | Finding | Fix |
|---|---|---|
| 1 | Admin URL not proven to be our container | Preflight compares cluster `system_identifier` (URL vs `docker exec psql`); tested against another server |
| 2 | `?dbname=` / percent-encoding bypass name guards | Such URLs refused; tests |
| 3 | Drop prefix from env / DB_BASE | Namespace hard-coded in helper; DB_BASE removed; base never droppable; tests |
| 4 | Helper failures not counted; empty digests "identical"; missing baseline skipped; leftovers on abort | `must` aborts; 7-table digest check; baseline required; EXIT trap drops created siblings |
| 5 | Annotated tag ≠ HEAD | `rev-parse "$EXPECTED_SHA^{commit}"` |
| 6 | Max-CPU compared as strings | docker-stats sampler removed (noisy); CPU from cgroup `usage_usec` |
| 7 | Missing GNU time → generate never ran | Preflight requires GNU time (BSD only on macOS) |
| 8 | Snapshot floods with repo/venv files; no networks/images | `SNAP_EXCLUDE` (checkout + output); networks + images listed |
| 9 | memory.peak includes pytest; sparse samples; Docker-root free space unchecked | Per-phase `memory.current` sampler (0.2 s); memory.peak labelled; Docker-root gate |
| 10 | Passwords in argv | URLs via env only (`crash`, `pg-stats`); test |

## Completed
- PRs #1–#4 merged; #5 (generation), #6 (COPY loader + ledger + smoke), #7 (validate) open, each reviewed + fixed.
- Dev DB `ecommerce_oltp` recreated (user-approved): clean smoke run `run_20261017T143356Z_a54aba`, validate 12/12.
- This task: `load.resume`, `load.status` (`RunStatus`), `load.world_for_planned`; shared `db.resolve_run` / `db.RunLookupError` (validate uses it too); `status` and `resume` CLI commands.

## Key decisions
- `resume` takes the same session advisory lock as `generate`: holding it proves no live writer, so a `RUNNING` run is stale (crash). Caveat: a vanished client's session can hold the lock until TCP keepalive ends it; the holder PID is shown so it can be terminated.
- Rows are a pure function of `World` + `DATA_VERSION` + `chunk_rows`. `resume_fields` (0003; shape CHECK in 0004) records all `World` fields + `data_version`, `chunk_rows`, `application_schema`. Resume **rebuilds the World from the record** (seed/clock/count edits can't change it) and refuses, naming the field, only if `data_version`, `chunk_rows` or `application_schema` differ. A future generation input must be a World field → recorded automatically (tested). `config_sha256` stays as provenance; pre-0003 runs refused.
- `DATA_VERSION` must be bumped when generated rows change; golden test = `GOLDEN[DATA_VERSION]` over a normal + a sparse world (sparse forces `_probe`'s fallback `_scan`, tested). Never edit an entry for a data change. Residual risk: nothing in code forces the bump.
- Sets run back to `RUNNING` (clears finished_at; keeps last error, a new failure overwrites it), then the same `_execute` path as generate: `write_chunks` skips ledgered chunks, `_complete`, `_fail` on error.
- `resume` on a COMPLETED run: no-op, exit 0. `generate` still refuses a DB that holds any run.
- `status` is read-only: reads `pg_locks` (bigint key → classid = high 32 bits, objid = low 32, objsubid = 1) before and after reading the run, never takes the lock. Shows progress (ledger rows / planned rows), last error, config + version match, lock-holder PID or "crashed".
- Lock refusal names the holder PID + `pg_terminate_backend(pid)` (dead client's lingering session after power loss).
- Exit codes: status 0 / 2 config / 3 couldn't read; resume 0 / 1 failed or refused / 2 config / 3 no run or unknown run. All CLI commands share `_connect_cfg`.

## Review (`/code-review high`)
| # | Finding | Fix |
|---|---|---|
| 1 | generate's refusal still said `resume` isn't implemented | Points to `ecomgen resume`; test |
| 2 | status lock check vs state read race → false "crashed" | Lock checked before and after reading the run (not deterministically testable) |
| 3 | Malformed planned counts → raw KeyError traceback | LoadError; test |
| 4 | Dead client's lingering session holds the lock (hours), no way to tell | status + refusal show holder PID and `pg_terminate_backend`; test |
| 5 | Resume cleared the original error | Error kept; test |
| 6 | status didn't warn on generator-version mismatch | status shows `resume_mismatch` (which inputs differ) for unfinished runs; test |
| 7 | Raw config sha256 / exact version too strict for crash recovery | `resume_fields` + `generate.DATA_VERSION`; World rebuilt from the record |

Focused second review (`/code-review medium`, resume compatibility): DATA_VERSION bump not enforced → GOLDEN comment fixed, `max(GOLDEN) == DATA_VERSION` (residual risk noted); hand-picked fields → record every World field + rebuild World from record; planned vs record duplication removed (`world_for_planned` gone); golden misses fallback path → sparse world + `_scan` coverage test; status crash on non-dict planned → guarded; no schema constraint → 0004 CHECK; JSON round-trip compare → both sides normalized; clock_start untested → covered by the accept-edits test.
| 8 | resume exit codes inconsistent with status/validate | Lookup errors exit 3; docstring; test |
| 9 | Config-loading block duplicated in 5 commands | All use `_connect_cfg` (test type aliases kept per module: tests/ isn't a package) |

## Files modified (this task)
`src/ecomgen/{load,db,cli,validate,generate}.py`, `src/ecomgen/migrations/{0003_run_resume_fields,0004_resume_fields_shape}.sql`, `tests/test_generate.py`, `tests/integration/{test_resume,conftest,test_load,test_validate,test_init_db}.py`, `README.md`, `TASKS.md`, this file.

## Test results (macOS, Python 3.12.14, Docker postgres:17.11)
- With DB: **123 passed, 0 failed, 0 skipped** (~22s). Without DB: 50 passed, 73 skipped (listed). mypy clean (16 files). No leftover test DBs.
- Real hard crash: child process SIGKILLs itself before orders chunk 2 → run RUNNING, lock released, status says crashed, progress orders 600/1000; `resume` writes 2, skips 7 → per-table md5 identical to an uninterrupted run; validate all OK; second resume no-op.
- FAILED run resumes (1 written, 8 skipped), counts from ledger = table counts, first error kept.
- Resume accepts seed + clock_start + clock_end + counts + bulk_copy + comment edits and a package version bump → identical to an uninterrupted run. Refusals: chunk_rows / application_schema (named), other DATA_VERSION, malformed record, pre-0003 run, lock held (PID shown), no run / unknown run; non-object resume_fields rejected by CHECK.
- Dev DB: `init-db` applied 0003 then 0004 over existing data (0003 kept byte-identical to what was applied); `status` COMPLETED; validate 12/12. Its run has `resume_fields` NULL (irrelevant: COMPLETED).

## Known failures / limitations
- No reset command (start over = recreate DB/schemas manually).
- `workers` unused (single process). No regeneration-vs-DB comparison in validate.
- No CI (no workflows); not run on Ubuntu; Phase 0 inventory pending.

## Next step
User answers the open decisions → user runs Step 0 inventory on Ubuntu → execute runbook at `v0.1.0-homelab` only with approval (`EXPECTED_SHA=v0.1.0-homelab`) → record results in `docs/BENCHMARKS.md` via PR. Nothing has run on Ubuntu yet.
