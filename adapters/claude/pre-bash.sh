#!/usr/bin/env bash
# Claude Code PreToolUse hook for Bash. Cheap keyword pre-filter, then the python guard.
set -u
input=$(cat)
# Pre-filter on the command text only, so cwd and session fields never decide the path.
# No "command": key found -> fall through to python (fail closed).
cmd=${input#*\"command\":}
[ "$cmd" = "$input" ] && cmd=test
cmd=${cmd%\"\},*}  # drop the sibling fields after tool_input (last `"},`); no match leaves it whole
case "$cmd" in
  *test*|*vitest*|*jest*|*push*|*commit*|*rm*|*docker*|*prisma*|*generate*|*install*|*build*|*playwright*|*logs*|*yarn*|*bun*|*npx*|*npm*) ;;
  *) exit 0 ;;
esac
if ! command -v python3 >/dev/null 2>&1; then
  echo "hardrail: python3 not found, guard disabled" >&2
  exit 0
fi
printf '%s' "$input" | exec python3 -I -S "$(dirname "$0")/pre_bash.py"
