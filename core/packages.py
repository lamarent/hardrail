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


_YAML_KEY = re.compile(r'^\s*(["\']?)packages\1\s*:(.*)$')


def _strip_comment(line):
    """Drop a trailing `# comment` that is not inside quotes."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in '"\'':
            quote = ch
        elif ch == '#' and (i == 0 or line[i - 1] in ' \t'):
            return line[:i]
    return line


def _unquote(word):
    word = word.strip()
    if len(word) >= 2 and word[0] == word[-1] and word[0] in '"\'':
        return word[1:-1]
    return word


def _yaml_globs(text):
    """`packages:` from pnpm-workspace.yaml: a block list, an inline list or a multi-line flow list."""
    lines = [_strip_comment(line).rstrip() for line in text.splitlines()]
    first = next((line for line in lines if line.strip()), '')
    top = len(first) - len(first.lstrip())  # only keys at the first key's indent are top level
    for i, line in enumerate(lines):
        m = _YAML_KEY.match(line)
        if not m or len(line) - len(line.lstrip()) != top:
            continue
        rest = m.group(2).strip()
        if rest.startswith('['):
            flow = ' '.join([rest] + lines[i + 1:])
            body = flow[1:flow.index(']')] if ']' in flow else ''
            return [g for g in (_unquote(x) for x in body.split(',')) if g]
        if rest:
            return []
        globs = []
        for item in lines[i + 1:]:
            item = item.strip()
            if not item:
                continue
            if item != '-' and not item.startswith('- '):
                break
            glob_ = _unquote(item[1:])
            if glob_:
                globs.append(glob_)
        return globs
    return []


def _workspace_globs(repo_root):
    try:
        with open(os.path.join(repo_root, "pnpm-workspace.yaml"), encoding="utf-8") as f:
            globs = _yaml_globs(f.read())
    except (OSError, UnicodeDecodeError):
        globs = []
    if not globs:
        data = _read_json(os.path.join(repo_root, "package.json")) or {}
        ws = data.get("workspaces")
        if isinstance(ws, dict):
            ws = ws.get("packages")
        globs = [g for g in ws if isinstance(g, str)] if isinstance(ws, list) else []
    return globs


def find_workspace_root(cwd):
    """Nearest ancestor of cwd (inclusive) with pnpm-workspace.yaml or a package.json that
    has a `workspaces` key, else None."""
    cur = os.path.abspath(cwd)
    while True:
        if os.path.isfile(os.path.join(cur, "pnpm-workspace.yaml")):
            return cur
        if "workspaces" in (_read_json(os.path.join(cur, "package.json")) or {}):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _by_name(name, repo_root):
    for pattern in _workspace_globs(repo_root):
        for d in sorted(glob.glob(os.path.join(repo_root, pattern))):
            pkg = _package_at(d) if os.path.isdir(d) else None
            if pkg and pkg.name == name:
                return pkg
    return None


def _filter_package(flt, repo_root, cwd):
    """Package a single `--filter` value selects; "all" for selectors and lookup misses."""
    braced = re.fullmatch(r'\{([^{}]+)\}', flt)
    if braced:
        flt = braced.group(1)
        if flt.startswith('/') or flt == '.':
            pass
        elif not flt.startswith(('./', '../')):
            flt = './' + flt
    if any(x in flt for x in _SELECTOR) or '!' in flt or '{' in flt or '^' in flt:
        return "all"
    if flt == '.':
        pkg = _ancestor_package(cwd, repo_root) if _within(cwd, repo_root) else None
    elif flt.startswith(('./', '../', '/')) or ('/' in flt and '@' not in flt):
        d = os.path.normpath(os.path.join(cwd if flt.startswith('.') else repo_root, flt))
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
    cds = _cd_targets(simple, prev) if target is None else []
    if filters:
        if target is not None:
            base = os.path.join(base, _expand(target))
        for d in cds:
            if d == "-":
                return "all"
            base = os.path.normpath(os.path.join(base, _expand(d)))
        return _filter_package(filters[0], repo_root, base)
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
