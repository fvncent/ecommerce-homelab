# Session Handoff

Update before compaction or ending a significant session. Overwrite; git history keeps the old ones.

**Updated:** 2026-10-17
**Branch / commit:** `chore/claude-workflow` (see PR)

## Current phase and task
Phase 0 (Inventory) — Claude Code workflow setup.

## Completed
- Added `CLAUDE.md` (permanent rules) and this handoff file.
- Fixed `.gitignore` (it contained a literal shell heredoc, so nothing was ignored); now also ignores `.claude/settings.local.json`.

## Key decisions
- Ponytail plugin at `lite` level; project non-negotiables in `CLAUDE.md` take precedence.
- No project-specific Skills yet; add one only when a procedure repeats (e.g. migrations).

## Files modified
`.gitignore`, `CLAUDE.md`, `docs/SESSION_HANDOFF.md`

## Test results
No application code yet; nothing to test.

## Known failures / limitations
- Phase 0 hardware inventory (`scripts/inspect_ubuntu_hardware.sh`) not yet run on Ubuntu.
- `*.jsonl` is ignored globally; sample fixtures will need a `!tests/fixtures/**` exception.

## Next step
Phase 1 task 1: Python package skeleton (`pyproject.toml`, locked deps, pytest + type checker, `.env.example`).
