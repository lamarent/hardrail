"""Claude Code PreToolUse shim: stdin JSON -> core.guard.decide -> exit code."""
import json
import os
import sys

# Two layouts: copied (.claude/hardrail/{core/,pre_bash.py}) and repo (adapters/claude/pre_bash.py).
_here = os.path.dirname(os.path.abspath(__file__))
for _d in (_here, os.path.dirname(os.path.dirname(_here))):
    if os.path.isfile(os.path.join(_d, "core", "guard.py")):
        sys.path.insert(0, _d)
        break

try:
    from core.config import DEFAULTS, find_root, load
    from core.guard import decide
    event = json.load(sys.stdin)
    cwd = event.get("cwd") or os.getcwd()
    root = find_root(cwd)
    d = decide(event["tool_input"]["command"], cwd, load(root) if root else DEFAULTS)
except Exception as e:  # fail open: an internal error must never block the agent
    sys.stderr.write("hardrail: guard error: %s\n" % " ".join(str(e).split())[:200])
    sys.exit(0)
if d.action == "block":
    sys.stderr.write("BLOCKED [%s]: %s\n" % (d.rule, d.message))
    sys.exit(2)
if d.action == "warn":
    sys.stderr.write("WARN [%s]: %s\n" % (d.rule, d.message))
