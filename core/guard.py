"""Guard decision: allow, warn or block a shell command before an agent runs it."""

import os
import re
import shlex

from core.argv import skip_manager_opts, strip_env_and_wrappers
from core.config import find_root
from core.packages import Package, resolve_package
from core.shell import ParseError, Simple, split_commands

_PATH_ARG = re.compile(r'\.(test|spec)\.(ts|tsx|js|jsx)$')
_SHORT_F = re.compile(r'^-[A-Za-z]*f[A-Za-z]*$')
_RUNNERS = ('vitest', 'jest')
_PKG_MANAGERS = ('pnpm', 'npm', 'yarn', 'bun')
_GIT_VALUE_OPTS = ('-C', '-c', '--git-dir', '--work-tree', '--namespace', '--exec-path',
                   '--super-prefix', '--config-env')
_RUN_FLAGS = ('--run', 'run', '--watch=false', '--no-watch')
_WATCH_FLAGS = ('--watch', '--watchAll')
_E2E_WATCH = ('--ui', '--watch', '--debug')
_PLACEHOLDER = '<path/to/file.test.ts>'


class Decision:
    # action: "allow" | "warn" | "block"
    # rule: force_push | chained_gated | watch_mode | unscoped_test | heavy | unparsable | ""
    # message: one line, ends with the corrected command when one exists
    __slots__ = ("action", "rule", "message")

    def __init__(self, action, rule, message):
        self.action = action
        self.rule = rule
        self.message = message

    def __eq__(self, other):
        return isinstance(other, Decision) and (self.action, self.rule, self.message) == \
            (other.action, other.rule, other.message)

    __hash__ = None

    def __repr__(self):
        return "Decision(action=%r, rule=%r, message=%r)" % (self.action, self.rule, self.message)


ALLOW = Decision("allow", "", "")


def _glob_match(pattern, text):
    """Full-match glob: `*` spans anything, `?` is one character, the rest is literal (no `[seq]`)."""
    parts = (".".join(re.escape(q) for q in p.split("?")) for p in pattern.split("*"))
    return re.fullmatch(".*".join(parts), text, re.S) is not None


_strip_env = strip_env_and_wrappers
_skip_opts = skip_manager_opts


def _strip_git(argv):
    """['/usr/bin/git', '--git-dir', 'x', 'push', ...] -> ['git', 'push', ...]."""
    if not argv or os.path.basename(argv[0]) != 'git':
        return argv
    i = 1
    while i < len(argv) and argv[i].startswith('-'):
        i += 2 if argv[i] in _GIT_VALUE_OPTS else 1
    return ['git'] + argv[i:]


def _cmd(s):
    c = _strip_git(_strip_env(s.argv)[1])
    return [os.path.basename(c[0])] + c[1:] if c else c


def _is_script(name):
    return (name == 'test' or name.startswith('test:')) and name != 'test:e2e'


_SPLIT_CACHE = {}


def _split_cached(cmd):
    if cmd not in _SPLIT_CACHE:
        _SPLIT_CACHE[cmd] = shlex.split(cmd)
    return list(_SPLIT_CACHE[cmd])


def _configured_tests(config):
    cmds = [config.get('defaults', {}).get('test')]
    for pkg in config.get('packages', {}).values():
        if isinstance(pkg, dict):
            cmds.append(pkg.get('test'))
    return [_split_cached(c) for c in cmds if isinstance(c, str) and c.strip()]


def _direct(rest):
    if rest and os.path.basename(rest[0]) in _RUNNERS:
        return os.path.basename(rest[0]), rest[1:]
    return None


def _runner(argv, config):
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
        if base == 'yarn' and rest[0] == 'workspace' and len(rest) > 2:
            rest = _skip_opts(rest[2:]) or ['']
        elif base == 'yarn' and rest[:2] == ['workspaces', 'foreach']:
            rest = _skip_opts(rest[2:]) or ['']
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


def _has_path(args):
    return any(not a.startswith('-') and ('/' in a or _PATH_ARG.search(a)) for a in args)


def _first_path(args):
    for a in args:
        if not a.startswith('-') and ('/' in a or _PATH_ARG.search(a)):
            return a
    return None


def _result(action, rule, msg, config):
    if action == "block" and config.get("guard", {}).get("mode") == "warn":
        action = "warn"
    return Decision(action, rule, msg)


def _unparsable(config, why=None):
    return _result("block", "unparsable", why or "cannot parse command; split it or quote it plainly", config)


def rule_force_push(simples, config, cwd="."):
    for s in simples:
        c = _cmd(s)
        if c[:2] != ['git', 'push']:
            continue
        for a in c[2:]:
            if a in ('--force', '--mirror') or _SHORT_F.match(a) or (a.startswith('+') and len(a) > 1):
                return _result("block", "force_push",
                               "force push is blocked; use git push --force-with-lease", config)
    return None


def _is_gated(s, config):
    c = _cmd(s)  # argv[0] compared by basename
    for prefix in config["guard"]["gatedActions"]:
        p = prefix.split()
        if p and c[:len(p)] == p:
            return True
    return False


def rule_chained_gated(simples, config, cwd="."):
    chains = {}
    for s in simples:
        chains.setdefault(s.payload_of, []).append(s)
    for chain in chains.values():
        if len(chain) < 2:
            continue
        gated = next((s for s in chain if _is_gated(s, config)), None)
        if gated is not None:
            return _result("block", "chained_gated",
                           f"gated action chained with other commands; run `{gated.raw}` as its own call",
                           config)
    return None


def _explicit_watch(args):
    for a in args:
        name, eq, val = a.partition('=')
        if name in _WATCH_FLAGS and (not eq or val not in ('false', '0')):
            return True
    return False


def _without(argv, drop):
    return [a for a in argv if a not in drop and a.partition('=')[0] not in drop]


def _watch_fix(s, config):
    """The one-shot command for a watch-mode simple, or None when it does not watch."""
    env, argv = _strip_env(s.argv)
    if any(e.startswith('CI=') for e in env) and not _explicit_watch(argv):
        return None
    c = _strip_git(argv)
    if c[:3] == ['docker', 'compose', 'logs'] and ('-f' in c or '--follow' in c):
        rest = _without(c[3:], ('-f', '--follow'))
        tail = [] if any(a.startswith('--tail') or a == '-n' for a in rest) else ['--tail=200']
        return "use " + ' '.join(c[:3] + tail + rest)
    if any(os.path.basename(a) in ('playwright', 'test:e2e') for a in c) and any(a in _E2E_WATCH for a in c):
        return "run it without the flag: " + ' '.join(_without(argv, _E2E_WATCH))
    hit = _runner(argv, config)
    if not hit:
        return None
    name, args = hit
    explicit = _explicit_watch(args)
    path = _first_path(args) or _PLACEHOLDER
    if name == 'jest':
        if not explicit:
            return None
        k = next(i for i, a in enumerate(argv) if os.path.basename(a) == 'jest')
        rest = [a for a in _without(argv[k + 1:], _WATCH_FLAGS) if a != '--']
        return "run once: " + ' '.join(argv[:k + 1] + ['--watchAll=false'] + rest)
    if name == 'vitest':
        if not explicit and any(a in _RUN_FLAGS for a in args):
            return None
        return f"run once: {config['defaults']['test']} {path}"
    if name.startswith('test:') and name.endswith('watch'):
        return f"run the package's `test` script with a path: {config['defaults']['test']} {path}"
    if explicit:
        return f"drop --watch and run once: {config['defaults']['test']} {path}"
    return None


def rule_watch_mode(simples, config, cwd="."):
    for s in simples:
        fix = _watch_fix(s, config)
        if fix:
            return _result("block", "watch_mode", f"watch mode never exits; {fix}", config)
    return None


def _own_path_token(script):
    return any('/' in t or _PATH_ARG.search(t) for t in script.split() if not t.startswith('-'))


def _unscoped_decision(s, prev, name, args,
                       config, cwd):
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
        rel = os.path.relpath(pkg.root, os.path.abspath(cwd))
        path = _first_path(args) or _PLACEHOLDER
        fix = f"pnpm exec vitest --run {path}" if rel == '.' else f"pnpm -C {rel} exec vitest --run {path}"
        return _result("block", "unscoped_test",
                       f"the package test script already has a path, so extra paths do not scope it; use: {fix}",
                       config)
    if _has_path(args):
        return None
    fix = f"{base} {_first_path(args) or _PLACEHOLDER}"
    return _result("block", "unscoped_test", f"unscoped test run; scope it: {fix}", config)


def rule_unscoped_test(simples, config, cwd="."):
    for i, s in enumerate(simples):
        hit = _runner(_strip_env(s.argv)[1], config)
        if not hit:
            continue
        prev = [p for p in simples[:i] if p.payload_of == s.payload_of]
        d = _unscoped_decision(s, prev, hit[0], hit[1], config, cwd)
        if d:
            return d
    return None


def _heavy_form(argv):
    if argv and os.path.basename(argv[0]) in _PKG_MANAGERS + ('npx', 'bunx'):
        argv = _skip_opts(argv[1:])
        if argv[:1] in (['run'], ['exec'], ['dlx']):
            argv = argv[1:]
    return ' '.join(argv)


def rule_heavy(simples, config, cwd="."):
    for s in simples:
        form = _heavy_form(_cmd(s))
        if any(_glob_match(g, form) for g in config.get("heavy", [])):
            return Decision("warn", "heavy",
                            f"heavy command; run it outside the agent loop or in a background task: {s.raw}")
    return None


RULES = [rule_force_push, rule_chained_gated, rule_watch_mode,
                     rule_unscoped_test, rule_heavy]


def decide(command, cwd, config):
    try:
        simples = split_commands(command)
    except ParseError as e:
        return _unparsable(config, str(e) if str(e).startswith("payload too large") else None)
    for rule in RULES:
        d = rule(simples, config, cwd)
        if d:
            return d
    return ALLOW
