"""Workspace package resolution: which package does a test command target?"""
from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Union

from core.shell import Simple

_ENV = re.compile(r'^[A-Za-z_]\w*=')
_WRAPPERS = ('sudo', 'time', 'env', 'nohup', 'command', 'exec')
_MANAGERS = ('pnpm', 'npm', 'yarn', 'bun')
_ALL_FLAGS = ('-r', '--recursive', '--parallel')
_DIR_FLAGS = ('-C', '--dir')
_FILTER_FLAGS = ('--filter', '-F')
_VALUE_FLAGS = ('--prefix', '--cwd', '--filter-prod')
_SELECTOR = ('*', '...', '[')


@dataclass
class Package:
    name: str
    root: str
    test_script: Optional[str]


def _expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _package_at(root: str) -> Optional[Package]:
    data = _read_json(os.path.join(root, "package.json"))
    if data is None:
        return None
    scripts = data.get("scripts")
    script = scripts.get("test") if isinstance(scripts, dict) else None
    name = data.get("name")
    return Package(name if isinstance(name, str) else os.path.basename(root),
                   root, script if isinstance(script, str) else None)


def _ancestor_package(start: str) -> Optional[Package]:
    cur = os.path.abspath(start)
    while True:
        pkg = _package_at(cur)
        if pkg:
            return pkg
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _workspace_globs(repo_root: str) -> List[str]:
    globs: List[str] = []
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


def _by_name(name: str, repo_root: str) -> Optional[Package]:
    for pattern in _workspace_globs(repo_root):
        for d in sorted(glob.glob(os.path.join(repo_root, pattern))):
            pkg = _package_at(d) if os.path.isdir(d) else None
            if pkg and pkg.name == name:
                return pkg
    return None


def _manager_flags(argv: List[str]) -> List[str]:
    """Option tokens between the package manager and its subcommand."""
    i = 0
    while i < len(argv) and (_ENV.match(argv[i]) or os.path.basename(argv[i]) in _WRAPPERS):
        i += 1
    if i >= len(argv) or os.path.basename(argv[i]) not in _MANAGERS:
        return []
    out: List[str] = []
    i += 1
    while i < len(argv) and argv[i].startswith('-'):
        out.append(argv[i])
        if argv[i] in _DIR_FLAGS + _FILTER_FLAGS + _VALUE_FLAGS and i + 1 < len(argv):
            i += 1
            out.append(argv[i])
        i += 1
    return out


def resolve_package(simple: Simple, cwd: str, config: dict, repo_root: str,
                    prev: Optional[Simple] = None) -> Union[Package, None, str]:
    """Package a test command targets; "all" when it spans the workspace; None if unknown."""
    flags = _manager_flags(simple.argv)
    base = os.path.abspath(cwd)
    target: Optional[str] = None
    filter_name: Optional[str] = None
    i = 0
    while i < len(flags):
        f = flags[i]
        value = flags[i + 1] if i + 1 < len(flags) else ""
        if f in _ALL_FLAGS:
            return "all"
        if f in _DIR_FLAGS:
            target = value
            i += 1
        elif f in _FILTER_FLAGS or f.startswith('--filter='):
            filter_name = f.split('=', 1)[1] if f.startswith('--filter=') else value
            i += 0 if f.startswith('--filter=') else 1
            if any(s in filter_name for s in _SELECTOR):
                return "all"
        i += 1
    if filter_name is not None:
        return _by_name(filter_name, repo_root)
    if target is not None:
        return _ancestor_package(os.path.join(base, _expand(target)))
    if prev is not None and len(prev.argv) >= 2 and prev.argv[0] == "cd" \
            and prev.payload_of == simple.payload_of:
        base = os.path.join(base, _expand(prev.argv[1]))
    return _ancestor_package(base)
