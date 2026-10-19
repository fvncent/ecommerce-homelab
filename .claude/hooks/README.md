# Claude Code safety hook (`guard.py`)

A PreToolUse hook on Bash, registered in `.claude/settings.json`. It is read only by Claude Code on the Mac. It
has no effect on `ecomgen`, on CI or on the Ubuntu deployment. There it is an inert file; `tests/test_guard.py`
only feeds it fixed strings.

## What it blocks
Each blocked command gets one reason, shown to Claude:
- `git tag -d/-f`; `git push --delete`, `:ref`, `--mirror`, `--prune`; force-pushing `main` or tags;
  `gh release|repo delete`; `gh api -X DELETE`.
- `docker rm|stop|kill|restart|down|pause|exec|update|rename` on `vlsim*` / `redpanda`; `docker … prune`.
- Any connection to port 55432 (VLSIM PostgreSQL).
- `rm|mv|shred|truncate`, `find … -delete` and `rsync --delete` under `/srv/homelab`.
- `ssh`, `scp`, `sftp`, `mosh`.
- `dropdb` / `DROP DATABASE` of `ecommerce_oltp`, `ecomgen_practice`, `vlsim*`, `tms*`.

Everything else passes, including throwaway test containers and databases, feature-branch pushes and creating
tags. Run `uv run pytest tests/test_guard.py` for the full allow/block list.

## Failure behaviour
- **Input it can't parse:** blocked if the raw input contains a risky word, otherwise allowed.
- **The script can't start, crashes or times out (10 s):** `onFailure: "block"` blocks the command.
- The script never runs commands itself. It reads stdin, matches regexes, and exits 0 or 2.

## Limitations
- It matches text, so it catches accidents, not deliberate evasion: variables (`$T`), `eval`, base64, aliases, a
  command written to a script file and then run, or a destructive SQL statement inside a here-doc or file.
- It can't tell which database a `DROP SCHEMA` / `TRUNCATE` hits. Only database names are protected.
- It covers the Bash tool only. Edit/Write to files are governed by the permission rules.
- The permission prompts, the auto-mode classifier and the GitHub rulesets (`main`, `v*-homelab` tags) remain the
  main protection.

## Disabling it safely
If the hook blocks normal work, Bash itself may be blocked. Use any of these; none needs Bash:
1. Ask Claude to edit `.claude/settings.json` with the Edit tool and remove the `PreToolUse` entry, then commit
   that change on a branch.
2. Turn it off temporarily from the `/hooks` menu, or set `"disableAllHooks": true` in
   `.claude/settings.local.json` (gitignored). Note that this also disables the Ponytail plugin's hooks.
3. Edit the file in any editor.

Re-enable by restoring the entry. Run `uv run pytest tests/test_guard.py` after any change to `guard.py`.
