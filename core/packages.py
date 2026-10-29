"""Workspace package resolution: which package does a test command target?"""

import glob
import json
import os
import re

from core.argv import manager_flags, strip_env_and_wrappers

_ALL_FLAGS = ('-r', '--recursive', '--parallel')
_ROOT_FLAGS = ('-w', '--workspace-root')
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
    except (OSError, ValueError, RecursionError):
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
    except (OSError, UnicodeDecodeError):
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


def _filter_package(flt, repo_root):
    if any(x in flt for x in _SELECTOR) or '!' in flt or '{' in flt or '^' in flt:
        return "all"
    if flt.startswith(('./', '../', '/')) or ('/' in flt and '@' not in flt):
        d = os.path.join(repo_root, flt)
        pkg = _package_at(d) if _within(d, repo_root) else None
    else:
        pkg = _by_name(flt, repo_root)
    return pkg or "all"


def _yarn_workspace(argv):
    """`yarn workspace <name> ...` -> name; `yarn workspaces foreach ...` -> "all"; else None."""
    argv = strip_env_and_wrappers(argv)[1]
    if not argv or os.path.basename(argv[0]) != 'yarn':
        return None
    rest = [a for a in argv[1:] if not a.startswith('-')]
    if rest[:1] == ['workspace'] and len(rest) > 1:
        return rest[1]
    if rest[:2] == ['workspaces', 'foreach']:
        return "all"
    return None


def _cd_targets(simple, prev):
    """Directories of every earlier `cd` in the same chain, in order; "-" for an unknown one."""
    prevs = prev if isinstance(prev, (list, tuple)) else ([prev] if prev is not None else [])
    out = []
    for p in prevs:
        if p.payload_of != simple.payload_of or not p.argv or p.argv[0] != "cd":
            continue
        args = [a for a in p.argv[1:] if a not in ('-L', '-P', '--')]
        out.append(args[0] if args else "~")
    return out


def resolve_package(simple, cwd, config, repo_root,
                    prev=None):
    """Package a test command targets; "all" when it spans the workspace or cannot be resolved.

    `prev` is the earlier simple command (or a list of them, in order); every `cd` among them
    in the same chain is applied to `cwd` before resolving.
    Fails closed: a lookup miss (unknown filter, directory without package.json, path outside
    the repo) returns "all", never None. None only means no package.json above cwd.
    """
    base = os.path.abspath(cwd)
    inside = _within(base, repo_root)
    target = None
    filters = []
    for flag, value in manager_flags(simple.argv):
        if flag in _ALL_FLAGS:
            return "all"
        if flag in _ROOT_FLAGS:
            target = repo_root
        elif flag in _DIR_FLAGS:
            target = value or ""
        elif flag in _FILTER_FLAGS:
            filters.append(value or "")
    ws = _yarn_workspace(simple.argv)
    if ws == "all":
        return "all"
    if ws:
        filters.append(ws)
    if len(filters) > 1:
        return "all"
    if filters:
        return _filter_package(filters[0], repo_root)
    cds = _cd_targets(simple, prev) if target is None else []
    if cds:
        for d in cds:
            if d == "-":
                return "all"
            base = os.path.normpath(os.path.join(base, _expand(d)))
        if inside and not _within(base, repo_root):
            return "all"
        return _ancestor_package(base, repo_root if inside else None)
    if target is not None:
        d = os.path.join(base, _expand(target))
        if inside and not _within(d, repo_root):
            return "all"
        return _package_at(d) or "all"
    return _ancestor_package(base, repo_root if inside else None)
