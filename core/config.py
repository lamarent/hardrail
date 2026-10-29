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
            "prisma migrate", "pnpm i", "npm i", "npm ci", "yarn install",
            "bun install", "bun add",
        ],
    },
    "heavy": ["build", "build:*", "turbo build", "vite build", "generate:*", "prisma", "prisma *",
              "playwright", "playwright *"],
    "worktree": {"required": False, "dir": ".worktrees", "include": []},
}

# Top-level keys the schema documents but the guard does not read; accepted silently.
_SCHEMA_ONLY_KEYS = ("$schema", "ci", "codegen", "conventionChecks", "modules", "language")


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
    and records why under the `_load_error` key. Unknown top-level keys are not fatal: the
    config is applied and `_load_warning` names them."""
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
    out = _merge(DEFAULTS, data)
    errors = []
    _check_types(DEFAULTS, out, "", errors)
    if errors:
        out["_load_error"] = "; ".join(errors)[:200]
    unknown = [k for k in data if k not in DEFAULTS and k not in _SCHEMA_ONLY_KEYS]
    if unknown:
        out["_load_warning"] = ("unknown config key %s ignored" % ", ".join(unknown))[:200]
    return out


def _check_types(defaults, cfg, prefix, errors):
    """Every dict/list/str default must keep its type (list items stay str); a wrong one
    reverts to the default and the reason goes to `errors`."""
    for key, default in defaults.items():
        if not isinstance(default, (dict, list, str)):
            continue
        value = cfg.get(key)
        bad = not isinstance(value, type(default))
        if not bad and isinstance(default, list) and all(isinstance(x, str) for x in default):
            bad = not all(isinstance(x, str) for x in value)
        if bad:
            cfg[key] = copy.deepcopy(default)
            errors.append("%s%s must be %s" % (prefix, key, type(default).__name__))
        elif isinstance(default, dict):
            _check_types(default, value, prefix + key + ".", errors)


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
