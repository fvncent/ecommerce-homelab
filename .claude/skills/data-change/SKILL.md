---
name: data-change
description: Checklist for changes that can alter generated ecomgen rows or files, or crash/resume and determinism behaviour - DATA_VERSION bump, golden entries, benchmark reference re-recording, step-chain vs generate equality, recovery tests, compatibility with existing runs. Use when touching generate.py, feeds.py, load.py, validate.py or migrations.
---

# data-change

Generation is a pure function of the World (seed, clock, counts), DATA_VERSION and chunk_rows.
`resume` and `advance` regenerate data and compare it with what is already committed, so any byte change must
be versioned.

## 1. Does output change?
- Run `uv run pytest tests/test_generate.py tests/test_feeds.py -q`. A failing `GOLDEN` / `FEED_GOLDEN` assertion
  means rows or files changed.
- If they changed on purpose: bump `generate.DATA_VERSION` and add a comment line saying what changed.
  - Then **add** a new entry to `GOLDEN` (rows) and `FEED_GOLDEN` (files). Never edit an existing entry.
  - If rows are unchanged, the new GOLDEN entry repeats the previous value.
- If they changed by accident: fix the code. Never update a golden to match.

## 2. Equality and recovery
- **Steps equal generate:** `tests/test_clock.py`, the step tests in `tests/test_feeds.py`,
  `tests/integration/test_advance.py` and `test_step_files.py` must pass. They cover any steps reaching `clock_end`
  giving exactly `generate`'s rows and records, half-open windows `(previous, as_of]`, and empty windows.
- **Crash/resume:** `tests/integration/test_resume.py` and the SIGKILL stages in `test_step_files.py` (db, csv,
  jsonl, place, manifest, complete) must pass. Add a stage when you add a write point.
- **10k end to end** (when step or file logic changed), in a throwaway DB:
  1. Write two configs that differ only in their three schema names, each with its own `ECOMGEN_DATA_ROOT`
     (directories created first).
  2. `init-db` both. Run `generate` on one; run daily `advance --to …` steps, then `--to end`, on the other.
  3. `validate` both.
  4. Compare the md5 of every table (with `SET TimeZone=UTC`) and the sorted record lines of every file.

## 3. Benchmark references (only if files or rows changed)
- References: `scripts/expected_{mvp,feed,smoke_feed}_digest.txt` and `expected_replay_originals.txt`.
- Re-record on macOS:
  1. In a throwaway PG container, create the role `bench_app` (no privileges) owning the empty databases
     `ecommerce_homelab_{smoke,test,crash}`. Create `$DATA_ROOT/benchmarks`.
  2. Run `RECORD_REFERENCES=1 ECOMGEN_BENCH_URL=… DATA_ROOT=… ECOMGEN_DATA_ROOT=… PG_CONTAINER=… PG_MAJOR=18
     bash scripts/homelab_benchmark.sh`. It should exit 3.
  3. Recreate the 3 throwaway DBs and run again without `RECORD_REFERENCES`. It must exit 0 with all PASS.
- `expected_mvp_digest.txt` must stay unchanged unless rows changed.

## 4. Compatibility
- State what existing runs of the old DATA_VERSION can still do with the new code: validate, status, resume,
  advance. Test the cases that are actually reachable (tests/integration). On Ubuntu, one-shot runs exist; chains
  exist only from v0.3.0 on.
- Migrations: add-only where possible, applied by `init-db` in one transaction. Never edit an applied migration
  (checksums are enforced): add `NNNN+1`.

## Report
Golden values added, the reference files changed (and the unchanged ones), equality numbers (rows, records),
benchmark result, and a compatibility statement. Mark anything not run.
