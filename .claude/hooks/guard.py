#!/usr/bin/env python3
"""PreToolUse hook for Bash: block a short list of irreversible commands against things this project must not
touch (release tags, main, VLSIM, /srv/homelab, persistent databases, the server). Exit 2 blocks the command and
shows the reason to Claude; exit 0 lets it through. It only reads stdin and matches text: it never runs anything.

Errors never authorize a risky command: if the input can't be parsed, the raw input is checked for risky words
(RISKY) and blocked if any appear; if this script can't run at all, `onFailure: "block"` in .claude/settings.json
blocks the command. Disable: see .claude/hooks/README.md.
shortcut: regexes on the command text catch accidents, not deliberate obfuscation (variables, base64, a script
file); the permission prompts and the auto-mode classifier remain the main defence.
"""

from __future__ import annotations  # runs under any python3 on PATH (macOS ships 3.9)

import json
import re
import sys

PROTECTED_DBS = r"(ecommerce_oltp|ecomgen_practice|vlsim\w*|tms\w*)"
RULES: list[tuple[str, str]] = [
    (r"\bgit\s+(-C\s+\S+\s+)?tag\b[^|;&]*\s(-[a-zA-Z]*[df][a-zA-Z]*|--delete|--force)\b",
     "deleting or force-moving a git tag (release tags are never moved)"),
    (r"\bgit\s+(-C\s+\S+\s+)?push\b[^|;&]*(\s--delete\b|\s-d\b|\s--mirror\b|\s--prune\b|\s:\S)",
     "deleting remote refs with git push"),
    (r"\bgit\s+(-C\s+\S+\s+)?push\b[^|;&]*(\s--force\b|\s--force-with-lease\b|\s-f\b|\s\+)[^|;&]*(\bmain\b|--tags|-homelab)",
     "force-pushing main or tags"),
    (r"\bgh\s+(release|repo)\s+delete\b|\bgh\s+api\b[^|;&]*(-X|--method)\s*DELETE",
     "deleting GitHub releases, repos or refs"),
    (r"\bdocker\b(?=[^|;&]*\b(rm|stop|kill|restart|down|pause|exec|update|rename)\b)(?=[^|;&]*(vlsim|redpanda))",
     "changing a VLSIM / Redpanda container"),
    (r"\bdocker\s+(system|volume|container|image|network)\s+prune\b",
     "docker prune (could remove VLSIM resources)"),
    (r"(:|-p\s*|--port[= ]|port=)55432\b", "connecting to the VLSIM PostgreSQL (port 55432)"),
    (r"\b(rm|mv|shred|truncate)\b[^|;&]*/srv/homelab|\bfind\b[^|;&]*/srv/homelab[^|;&]*-delete|"
     r"\brsync\b[^|;&]*--delete[^|;&]*/srv/homelab",
     "removing or moving files under /srv/homelab"),
    (r"(^|[;&|(]\s*)(ssh|scp|sftp|mosh)\s", "connecting to a server (the user runs Ubuntu commands by hand)"),
    (rf"\bdropdb\b[^|;&]*\b{PROTECTED_DBS}\b|\bdrop\s+database\b[^|;&]*\b{PROTECTED_DBS}\b",
     "dropping a persistent database"),
]
COMPILED = [(re.compile(p, re.IGNORECASE), why) for p, why in RULES]
# words that make unparseable input suspect enough to block (fallback only)
RISKY = re.compile(r"\b(tag|push|gh|docker|dropdb|drop|rm|mv|find|rsync|shred|truncate|ssh|scp|sftp|mosh)\b|55432|"
                   r"/srv/homelab", re.IGNORECASE)


def blocked(command: str) -> str | None:
    """The reason a command is blocked, or None."""
    for pattern, why in COMPILED:
        if pattern.search(command):
            return why
    return None


def main() -> int:
    raw = sys.stdin.read()
    try:
        command = json.loads(raw)["tool_input"]["command"]
        if not isinstance(command, str):
            raise TypeError("command is not a string")
        why = blocked(command)
    except Exception:  # unparseable: allow only input without risky words, so normal work isn't stopped
        why = "the hook could not parse its input and it contains a risky word" if RISKY.search(raw) else None
    if why:
        print(f"Blocked by .claude/hooks/guard.py: {why}. Ask the user to run it themselves if it is really "
              "intended.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
