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
    config = load(root) if root else DEFAULTS
    for key, fmt in (("_load_error", "config ignored (%s), using defaults"), ("_load_warning", "%s")):
        if config.get(key):
            sys.stderr.write("hardrail: %s\n" % (fmt % config[key]))
    d = decide(event["tool_input"]["command"], cwd, config)
except Exception as e:  # fail open: an internal error must never block the agent
    sys.stderr.write("hardrail: guard error: %s\n" % " ".join(str(e).split())[:200])
    sys.exit(0)
out = {"block": ("BLOCKED", 2), "warn": ("WARN", 0)}.get(d.action)
if out:
    sys.stderr.write("%s [%s]: %s\n" % (out[0], d.rule, d.message))
    sys.exit(out[1])
