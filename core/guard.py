"""Guard decision: allow, warn or block a shell command before an agent runs it."""
from __future__ import annotations

import fnmatch
import os
import re
import shlex
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from core.config import find_root
from core.packages import Package, resolve_package
from core.shell import ParseError, Simple, split_commands

_ENV = re.compile(r'^[A-Za-z_]\w*=')
_PATH_ARG = re.compile(r'\.(test|spec)\.(ts|tsx|js|jsx)$')
_SHORT_F = re.compile(r'^-[A-Za-z]*f[A-Za-z]*$')
_RUNNERS = ('vitest', 'jest')
_WRAPPERS = ('sudo', 'time', 'env', 'nohup', 'command', 'exec')
_PKG_MANAGERS = ('pnpm', 'npm', 'yarn', 'bun')
_VALUE_FLAGS = ('-C', '--filter', '-F', '--dir', '--prefix', '--cwd')
_RUN_FLAGS = ('--run', 'run', '--watch=false', '--no-watch')
_E2E_WATCH = ('--ui', '--watch', '--debug')
_PLACEHOLDER = '<path/to/file.test.ts>'


@dataclass
class Decision:
    action: str   # "allow" | "warn" | "block"
    rule: str     # force_push | chained_gated | watch_mode | unscoped_test | heavy | unparsable | ""
    message: str  # one line, ends with the corrected command when one exists


ALLOW = Decision("allow", "", "")
Rule = Callable[[List[Simple], dict, str], Optional[Decision]]


def _strip_env(argv: List[str]) -> Tuple[List[str], List[str]]:
    """Split leading env assignments and wrappers (sudo, time, env...) from the command."""
    i = 0
    while i < len(argv) and (_ENV.match(argv[i]) or os.path.basename(argv[i]) in _WRAPPERS):
        i += 1
    return argv[:i], argv[i:]


def _strip_git(argv: List[str]) -> List[str]:
    """['git', '-C', 'x', 'push', ...] -> ['git', 'push', ...]."""
    if not argv or argv[0] != 'git':
        return argv
    i = 1
    while i < len(argv) and argv[i].startswith('-'):
        i += 2 if argv[i] in ('-C', '-c') else 1
    return ['git'] + argv[i:]


def _cmd(s: Simple) -> List[str]:
    return _strip_git(_strip_env(s.argv)[1])


def _skip_opts(args: List[str]) -> List[str]:
    i = 0
    while i < len(args) and args[i].startswith('-'):
        i += 2 if args[i] in _VALUE_FLAGS else 1
    return args[i:]


def _is_script(name: str) -> bool:
    return (name == 'test' or name.startswith('test:')) and name != 'test:e2e'


def _configured_tests(config: dict) -> List[List[str]]:
    cmds = [config.get('defaults', {}).get('test')]
    for pkg in config.get('packages', {}).values():
        if isinstance(pkg, dict):
            cmds.append(pkg.get('test'))
    return [shlex.split(c) for c in cmds if isinstance(c, str) and c.strip()]


def _direct(rest: List[str]) -> Optional[Tuple[str, List[str]]]:
    if rest and os.path.basename(rest[0]) in _RUNNERS:
        return os.path.basename(rest[0]), rest[1:]
    return None


def _runner(argv: List[str], config: dict) -> Optional[Tuple[str, List[str]]]:
    """Detect a test runner. Returns (name, args after the runner token)."""
    if not argv:
        return None
    base = os.path.basename(argv[0])
    hit = _direct(argv)
    if hit:
        return hit
    if base in ('npx', 'bunx'):
        return _direct(_skip_opts(argv[1:]))
    if base in _PKG_MANAGERS:
        rest = _skip_opts(argv[1:])
        if not rest:
            return None
        hit = _direct(_skip_opts(rest[1:]) if rest[0] in ('exec', 'dlx') else rest)
        if hit:
            return hit
        if rest[0] == 'run' and len(rest) > 1:
            rest = rest[1:]
        if _is_script(rest[0]):
            return rest[0], rest[1:]
    if base == 'turbo' and argv[1:3] == ['run', 'test']:
        return 'test', argv[3:]
    if base == 'nx' and argv[1:2] == ['test']:
        return 'test', argv[2:]
    for cmd in _configured_tests(config):
        if argv[:len(cmd)] == cmd:
            return 'configured', argv[len(cmd):]
    return None


def _has_path(args: List[str]) -> bool:
    return any(not a.startswith('-') and ('/' in a or _PATH_ARG.search(a)) for a in args)


def _first_path(args: List[str]) -> Optional[str]:
    for a in args:
        if not a.startswith('-') and ('/' in a or _PATH_ARG.search(a)):
            return a
    return None


def _result(action: str, rule: str, msg: str, config: dict) -> Decision:
    if action == "block" and config.get("guard", {}).get("mode") == "warn":
        action = "warn"
    return Decision(action, rule, msg)


def _unparsable(config: dict) -> Decision:
    return _result("block", "unparsable", "cannot parse command; split it or quote it plainly", config)


def rule_force_push(simples: List[Simple], config: dict, cwd: str = ".") -> Optional[Decision]:
    for s in simples:
        c = _cmd(s)
        if c[:2] != ['git', 'push']:
            continue
        for a in c[2:]:
            if a == '--force' or _SHORT_F.match(a) or (a.startswith('+') and len(a) > 1):
                return _result("block", "force_push",
                               "force push is blocked; use git push --force-with-lease", config)
    return None


def _is_gated(s: Simple, config: dict) -> bool:
    c = _cmd(s)
    for prefix in config["guard"]["gatedActions"]:
        p = prefix.split()
        if p and c[:len(p)] == p:
            return True
    return False


def rule_chained_gated(simples: List[Simple], config: dict, cwd: str = ".") -> Optional[Decision]:
    chains = {}
    for s in simples:
        chains.setdefault(s.payload_of, []).append(s)
    for chain in chains.values():
        if len(chain) > 1 and any(_is_gated(s, config) for s in chain):
            return _result("block", "chained_gated",
                           "gated action chained with other commands; run it as its own command", config)
    return None


def _watch_mode(s: Simple, config: dict) -> bool:
    env, argv = _strip_env(s.argv)
    if any(e.startswith('CI=') for e in env):
        return False
    c = _strip_git(argv)
    if c[:3] == ['docker', 'compose', 'logs'] and ('-f' in c or '--follow' in c):
        return True
    if any(os.path.basename(a) in ('playwright', 'test:e2e') for a in c) and any(a in _E2E_WATCH for a in c):
        return True
    hit = _runner(argv, config)
    if not hit:
        return False
    name, args = hit
    if name in _RUNNERS:
        return not any(a in _RUN_FLAGS for a in args)
    return name.startswith('test:') and name.endswith('watch')


def rule_watch_mode(simples: List[Simple], config: dict, cwd: str = ".") -> Optional[Decision]:
    for s in simples:
        if _watch_mode(s, config):
            return _result("block", "watch_mode",
                           "watch mode never exits; run once, e.g. "
                           f"{config['defaults']['test']} {_PLACEHOLDER}", config)
    return None


def _own_path_token(script: str) -> bool:
    return any('/' in t or _PATH_ARG.search(t) for t in script.split() if not t.startswith('-'))


def _unscoped_decision(s: Simple, prev: Optional[Simple], name: str, args: List[str],
                       config: dict, cwd: str) -> Optional[Decision]:
    if '--help' in args:
        return None
    root = find_root(cwd) or cwd
    pkg = resolve_package(s, cwd, config, root, prev=prev)
    base = config['defaults']['test']
    if pkg == "all":
        fix = f"{base.replace('exec', '--filter <name> exec', 1)} {_first_path(args) or _PLACEHOLDER}"
        return _result("block", "unscoped_test",
                       f"this runs every workspace package; target one package: {fix}", config)
    if isinstance(pkg, Package) and name not in _RUNNERS and pkg.test_script \
            and _own_path_token(pkg.test_script):
        rel = os.path.relpath(pkg.root, os.path.abspath(root))
        path = _first_path(args) or _PLACEHOLDER
        fix = f"pnpm -C {rel} exec vitest --run {path}"
        return _result("block", "unscoped_test",
                       f"the package test script already has a path, so extra paths do not scope it; use: {fix}",
                       config)
    if _has_path(args):
        return None
    fix = f"{base} {_first_path(args) or _PLACEHOLDER}"
    return _result("block", "unscoped_test", f"unscoped test run; scope it: {fix}", config)


def rule_unscoped_test(simples: List[Simple], config: dict, cwd: str = ".") -> Optional[Decision]:
    for i, s in enumerate(simples):
        hit = _runner(_strip_env(s.argv)[1], config)
        if not hit:
            continue
        prev = simples[i - 1] if i > 0 else None
        d = _unscoped_decision(s, prev, hit[0], hit[1], config, cwd)
        if d:
            return d
    return None


def _heavy_form(argv: List[str]) -> str:
    if argv[:2] in (['npm', 'run'], ['pnpm', 'run'], ['pnpm', 'exec'], ['pnpm', 'dlx']):
        argv = argv[2:]
    elif argv[:1] in (['pnpm'], ['yarn'], ['npx']):
        argv = argv[1:]
    return ' '.join(argv)


def rule_heavy(simples: List[Simple], config: dict, cwd: str = ".") -> Optional[Decision]:
    for s in simples:
        form = _heavy_form(_cmd(s))
        if any(fnmatch.fnmatchcase(form, g) for g in config.get("heavy", [])):
            return Decision("warn", "heavy", f"heavy command; dispatch it to a runner agent: {form}")
    return None


RULES: List[Rule] = [rule_force_push, rule_chained_gated, rule_watch_mode,
                     rule_unscoped_test, rule_heavy]


def decide(command: str, cwd: str, config: dict) -> Decision:
    try:
        simples = split_commands(command)
    except ParseError:
        return _unparsable(config)
    for rule in RULES:
        d = rule(simples, config, cwd)
        if d:
            return d
    return ALLOW
