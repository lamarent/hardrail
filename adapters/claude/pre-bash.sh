#!/usr/bin/env bash
# Claude Code PreToolUse hook for Bash. Cheap keyword pre-filter, then the python guard.
set -u
input=$(cat)
[ -n "$input" ] || exit 0
s=$0
# Only one symlink level is resolved (a link to a link is not followed).
if [ -L "$s" ]; then
  s=$(readlink "$s")
  case "$s" in /*) ;; *) s="$(dirname "$0")/$s" ;; esac
fi
d=$(cd "$(dirname "$s")" && pwd)
[ -f "$d/pre_bash.py" ] || { echo "hardrail: pre_bash.py not found, guard disabled" >&2; exit 0; }
# Pre-filter scans everything after the first "command": key (keeps more text, never less).
# No "command": key found -> fall through to python (fail closed).
cmd=${input#*\"command\":}
[ "$cmd" = "$input" ] && cmd=test
# The keyword list MUST cover the first word (and subcommand) of every DEFAULTS
# guard.gatedActions and heavy entry; test/adapters/test_claude.py checks it.
# A project config can add gatedActions the list does not know: skip the fast path then.
[ -f "${CLAUDE_PROJECT_DIR:-.}/hardrail.config.json" ] && cmd=test
case "$cmd" in
  # Quotes, backslashes and expansions can split a keyword (pu''sh, $cmd); in the JSON
  # text a quote inside the command shows as \". A shell name may read piped text.
  *[\'\\\$\`]*|*'<('*|*sh*) ;;
  *test*|*vitest*|*jest*|*push*|*commit*|*reset*|*rebase*|*merge*|*stash*|*clean*|*gh*|*rm*|*docker*|*podman*|*turbo*|*tail*|*prisma*|*generate*|*install*|*build*|*playwright*|*logs*|*yarn*|*bun*|*npx*|*npm*) ;;
  *) exit 0 ;;
esac
if ! command -v python3 >/dev/null 2>&1; then
  echo "hardrail: python3 not found, guard disabled" >&2
  exit 0
fi
printf '%s' "$input" | python3 -I -S "$d/pre_bash.py"
rc=$?
[ "$rc" -eq 2 ] && exit 2
exit 0
