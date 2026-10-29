"""Config loader: reads hardrail.config.json and deep-merges it over DEFAULTS."""
import copy
import json
import os

CONFIG_NAME = "hardrail.config.json"

DEFAULTS = {
    "defaults": {"test": "pnpm exec vitest --run"},
    "packages": {},
    "guard": {
        "mode": "block",
        "gatedActions": [
            "git commit", "git push", "git reset", "git rebase", "git merge",
            "git stash", "git clean", "rm", "pnpm install", "pnpm add",
            "pnpm remove", "npm install", "yarn add", "docker", "gh pr",
            "prisma migrate",
        ],
    },
    "heavy": ["build", "generate:*", "prisma *", "playwright", "docker compose logs"],
    "worktree": {"required": False, "dir": ".worktrees", "include": []},
}


def _merge(base, override):
    """Dicts merge recursively; lists and scalars from override replace base."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _fallback(reason):
    out = copy.deepcopy(DEFAULTS)
    out["_load_error"] = " ".join(reason.split())[:200]
    return out


def load(repo_root):
    """Merged config. A broken file never disables the guard: it falls back to DEFAULTS
    and records why under the `_load_error` key."""
    path = os.path.join(repo_root, CONFIG_NAME)
    if not os.path.isfile(path):
        return copy.deepcopy(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return _fallback("%s: %s" % (CONFIG_NAME, e))
    if not isinstance(data, dict):
        return _fallback("%s is not a JSON object" % CONFIG_NAME)
    return _merge(DEFAULTS, data)


def find_root(cwd):
    """Nearest ancestor of cwd (inclusive) containing the config file, else None."""
    cur = os.path.abspath(cwd)
    while True:
        if os.path.isfile(os.path.join(cur, CONFIG_NAME)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent
