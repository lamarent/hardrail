"""Workspace package resolution: which package does a test command target?"""

import glob
import json
import os
import re

from core.shell import Simple

_ENV = re.compile(r'^[A-Za-z_]\w*=')
_WRAPPERS = ('sudo', 'time', 'env', 'nohup', 'command', 'exec')
_MANAGERS = ('pnpm', 'npm', 'yarn', 'bun')
_ALL_FLAGS = ('-r', '--recursive', '--parallel')
_ROOT_FLAGS = ('-w', '--workspace-root')
_LONG_VALUE = ('--dir', '--filter', '--filter-prod', '--prefix', '--cwd')
_DIR_FLAGS = ('-C', '--dir')
_FILTER_FLAGS = ('--filter', '-F', '--filter-prod')
_SELECTOR = ('*', '...', '[')


class Package:
    __slots__ = ("name", "root", "test_script")

    def __init__(self, name, root, test_script):
        self.name = name
        self.root = root
        self.test_script = test_script

    def __eq__(self, other):
        return isinstance(other, Package) and (self.name, self.root, self.test_script) == \
            (other.name, other.root, other.test_script)

    __hash__ = None

    def __repr__(self):
        return "Package(name=%r, root=%r, test_script=%r)" % (self.name, self.root, self.test_script)


def _expand(path):
    return os.path.expandvars(os.path.expanduser(path))


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _package_at(root):
    data = _read_json(os.path.join(root, "package.json"))
    if data is None:
        return None
    scripts = data.get("scripts")
    script = scripts.get("test") if isinstance(scripts, dict) else None
    name = data.get("name")
    return Package(name if isinstance(name, str) else os.path.basename(root),
                   root, script if isinstance(script, str) else None)


def _within(path, root):
    path, root = os.path.abspath(path), os.path.abspath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _ancestor_package(start, stop=None):
    """Nearest ancestor (inclusive) with a package.json; never climbs above `stop`."""
    cur = os.path.abspath(start)
    while True:
        pkg = _package_at(cur)
        if pkg:
            return pkg
        parent = os.path.dirname(cur)
        if parent == cur or (stop is not None and cur == os.path.abspath(stop)):
            return None
        cur = parent


def _workspace_globs(repo_root):
    globs = []
    try:
        with open(os.path.join(repo_root, "pnpm-workspace.yaml"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    inside = False
    for line in lines:
        if re.match(r'^packages\s*:', line):
            inside = True
        elif inside and line.strip().startswith('- '):
            globs.append(line.strip()[2:].strip().strip('"\''))
        elif inside and line.strip() and not line.startswith((' ', '\t', '#')):
            inside = False
    if not globs:
        data = _read_json(os.path.join(repo_root, "package.json")) or {}
        ws = data.get("workspaces")
        if isinstance(ws, dict):
            ws = ws.get("packages")
        globs = [g for g in ws if isinstance(g, str)] if isinstance(ws, list) else []
    return globs


def _by_name(name, repo_root):
    for pattern in _workspace_globs(repo_root):
        for d in sorted(glob.glob(os.path.join(repo_root, pattern))):
            pkg = _package_at(d) if os.path.isdir(d) else None
            if pkg and pkg.name == name:
                return pkg
    return None


def _parse_flags(argv):
    """(flag, value) pairs between the package manager and its subcommand.

    Handles `--flag value`, `--flag=value`, `-C=value`, and short clusters (`-rF value`).
    """
    i = 0
    while i < len(argv) and (_ENV.match(argv[i]) or os.path.basename(argv[i]) in _WRAPPERS):
        i += 1
    if i >= len(argv) or os.path.basename(argv[i]) not in _MANAGERS:
        return []
    out = []
    i += 1
    while i < len(argv) and argv[i].startswith('-') and argv[i] != '-':
        t = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else ""
        if t.startswith('--'):
            name, eq, val = t.partition('=')
            if name in _LONG_VALUE and not eq:
                val, i = nxt, i + 1
            out.append((name, val if (eq or name in _LONG_VALUE) else None))
        elif '=' in t:
            name, _, val = t.partition('=')
            out.append((name, val))
        else:
            for k, letter in enumerate(t[1:], start=2):
                if letter in 'FC':
                    rest = t[k:]
                    if not rest:
                        rest, i = nxt, i + 1
                    out.append(('-' + letter, rest))
                    break
                out.append(('-' + letter, None))
        i += 1
    return out


def _filter_package(flt, repo_root):
    if any(x in flt for x in _SELECTOR) or '!' in flt or '{' in flt or '^' in flt:
        return "all"
    if flt.startswith(('./', '../', '/')) or ('/' in flt and '@' not in flt):
        d = os.path.join(repo_root, flt)
        pkg = _package_at(d) if _within(d, repo_root) else None
    else:
        pkg = _by_name(flt, repo_root)
    return pkg or "all"


def resolve_package(simple, cwd, config, repo_root,
                    prev=None):
    """Package a test command targets; "all" when it spans the workspace or cannot be resolved.

    Fails closed: a lookup miss (unknown filter, directory without package.json, path outside
    the repo) returns "all", never None. None only means no package.json above cwd.
    """
    base = os.path.abspath(cwd)
    inside = _within(base, repo_root)
    target = None
    filters = []
    for flag, value in _parse_flags(simple.argv):
        if flag in _ALL_FLAGS:
            return "all"
        if flag in _ROOT_FLAGS:
            target = repo_root
        elif flag in _DIR_FLAGS:
            target = value or ""
        elif flag in _FILTER_FLAGS:
            filters.append(value or "")
    if len(filters) > 1:
        return "all"
    if filters:
        return _filter_package(filters[0], repo_root)
    if target is None and prev is not None and len(prev.argv) >= 2 and prev.argv[0] == "cd" \
            and prev.payload_of == simple.payload_of:
        base = os.path.join(base, _expand(prev.argv[1]))
        if inside and not _within(base, repo_root):
            return "all"
        return _ancestor_package(base, repo_root if inside else None)
    if target is not None:
        d = os.path.join(base, _expand(target))
        if inside and not _within(d, repo_root):
            return "all"
        return _package_at(d) or "all"
    return _ancestor_package(base, repo_root if inside else None)
