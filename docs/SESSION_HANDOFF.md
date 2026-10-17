# Session Handoff

Update before compaction or ending a significant session. Overwrite; git history keeps the old ones.

**Updated:** 2026-10-17
**Branch / commit:** `feat/phase1-skeleton` (see `git log`)

## Current phase and task
Phase 1 (CLI + schema MVP) — task 1 done: package skeleton.

## Completed
- PR #1 merged: `CLAUDE.md`, this handoff, fixed `.gitignore`.
- uv-managed package `ecomgen` (src layout, Python ≥3.12, `uv.lock`), argparse CLI with `--version`, pytest + strict mypy, `.env.example`, README dev section.

## Key decisions
- Tooling: uv + hatchling, argparse (stdlib), pytest, mypy `strict`. No linter yet.
- Zero runtime deps for now; psycopg 3 arrives with `init-db`, PyYAML with config loading.
- Python 3.12 pinned (`.python-version`) to match Ubuntu 24.04's system Python.
- Ponytail plugin at `lite`; `CLAUDE.md` non-negotiables take precedence.

## Files modified
`pyproject.toml`, `uv.lock`, `.python-version`, `.env.example`, `src/ecomgen/{__init__,__main__,cli}.py`, `src/ecomgen/py.typed`, `tests/test_cli.py`, `README.md`, this file.

## Test results (macOS, Python 3.12.14)
- `uv run pytest`: 2 passed.
- `uv run mypy`: no issues in 4 files.

## Known failures / limitations
- Not run on Ubuntu yet; Phase 0 hardware inventory still pending.
- No CI workflow.
- `*.jsonl` ignored globally; fixtures will need a `!tests/fixtures/**` exception.

## Next step
Phase 1 task 2: config YAML loading + validation (seed, counts, chunk size, env var for DB URL) with tests for bad config.
