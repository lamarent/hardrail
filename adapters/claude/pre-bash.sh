#!/usr/bin/env bash
# Claude Code PreToolUse hook for Bash. Cheap keyword pre-filter, then the python guard.
set -u
input=$(cat)
# Pre-filter scans everything after the first "command": key (keeps more text, never less).
# No "command": key found -> fall through to python (fail closed).
cmd=${input#*\"command\":}
[ "$cmd" = "$input" ] && cmd=test
# The keyword list MUST cover the first word (and subcommand) of every DEFAULTS
# guard.gatedActions and heavy entry; test/adapters/test_claude.py checks it.
case "$cmd" in
  *test*|*vitest*|*jest*|*push*|*commit*|*reset*|*rebase*|*merge*|*stash*|*clean*|*gh*|*rm*|*docker*|*prisma*|*generate*|*install*|*build*|*playwright*|*logs*|*yarn*|*bun*|*npx*|*npm*) ;;
  *) exit 0 ;;
esac
if ! command -v python3 >/dev/null 2>&1; then
  echo "hardrail: python3 not found, guard disabled" >&2
  exit 0
fi
printf '%s' "$input" | exec python3 -I -S "$(dirname "$0")/pre_bash.py"
