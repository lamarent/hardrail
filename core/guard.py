"""Guard decision: allow, warn or block a shell command before an agent runs it."""

import os
import re
import shlex

from core.argv import XARGS_ERR, skip_manager_opts, strip_env_and_wrappers, xargs_writes_command
from core.config import find_root
from core.packages import Package, find_workspace_root, resolve_package
from core.shell import _SUBST_PH, ParseError, is_pipe, scan_opts, split_commands

_PATH_ARG = re.compile(r'\.(test|spec)\.(ts|tsx|js|jsx)$')
_SHORT_F = re.compile(r'^-[A-Za-z]*f[A-Za-z]*$')
_SHORT_D = re.compile(r'^-[A-Za-z]*d[A-Za-z]*$')
_RUNNERS = ('vitest', 'jest')
_PKG_MANAGERS = ('pnpm', 'npm', 'yarn', 'bun')
_GIT_VALUE_OPTS = ('-C', '-c', '--git-dir', '--work-tree', '--namespace', '--exec-path',
                   '--super-prefix', '--config-env')
_RUN_FLAGS = ('--run', 'run', '--watch=false', '--no-watch')
_WATCH_FLAGS = ('--watch', '--watchAll')
_E2E_WATCH = ('--ui', '--watch', '--debug')
_PLACEHOLDER = '<path/to/file.test.ts>'
# Runner options whose next word is a value, not a test path.
_RUNNER_VALUE_FLAGS = ('--config', '-c', '--root', '-r', '--dir', '--project', '--reporter', '--outputFile',
                       '--coverage.reporter', '-t', '--testNamePattern', '--rootDir', '--maxWorkers', '-w',
                       '--selectProjects', '--shard', '--pool', '--exclude', '--setupFiles',
                       '--testPathIgnorePatterns')
_JEST_PATH_FLAGS = ('--testPathPattern', '--testPathPatterns')  # their value is a path
_VITEST_SUBCMDS = ('related', 'bench', 'list')          # kept in the fix command
_VITEST_LEADING = ('run', 'watch', 'dev') + _VITEST_SUBCMDS  # a leading one is a subcommand
_ANY_PATTERN = ('', '.', '.*')                          # path patterns that match everything
_DOCKER_BASES = ('docker', 'docker-compose', 'podman')
_DOCKER_GLOBAL_VALUE = ('-H', '--host', '--context', '-c', '--config', '-l', '--log-level')
_COMPOSE_VALUE = ('-f', '--file', '-p', '--project-name', '--env-file', '--profile', '--project-directory')
_TAIL_VALUE = ('-n', '-c', '-s', '--lines', '--bytes', '--sleep-interval', '--pid', '--max-unchanged-stats')
_DELETE_MSG = "deleting a remote branch is blocked; ask before deleting remote refs"
_EXPANSION_MSG = "git push argument built from an expansion; write the refspec literally"
_FIND_EXEC = ('-exec', '-execdir', '-ok', '-okdir')


def _is_scope(word):
    """A path names something narrower than the cwd: not empty, `.`, `..`, `/` or `../..`."""
    norm = os.path.normpath(word) if word else ''
    return bool(word) and norm != '/' and any(p not in ('.', '..') for p in norm.split('/'))


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


_GLOB_CACHE = {}


def _glob_match(pattern, text):
    """Full-match glob: `*` spans anything, `?` is one character, the rest is literal (no `[seq]`)."""
    rx = _GLOB_CACHE.get(pattern)
    if rx is None:
        parts = (".".join(re.escape(q) for q in p.split("?")) for p in pattern.split("*"))
        rx = _GLOB_CACHE[pattern] = re.compile(".*".join(parts), re.S)
    return rx.fullmatch(text) is not None


def _strip_git(argv):
    """['/usr/bin/git', '--git-dir', 'x', 'push', ...] -> ['git', 'push', ...]."""
    if not argv or os.path.basename(argv[0]) != 'git':
        return argv
    i = 1
    while i < len(argv) and argv[i].startswith('-'):
        i += 2 if argv[i] in _GIT_VALUE_OPTS else 1
    return ['git'] + argv[i:]


def _cmd(s):
    c = _strip_git(strip_env_and_wrappers(s.argv)[1])
    return [os.path.basename(c[0])] + c[1:] if c else c


def _is_script(name):
    return (name == 'test' or name.startswith('test:')) and name != 'test:e2e'


_SPLIT_CACHE = {}


def _split_cached(cmd, key):
    if cmd not in _SPLIT_CACHE:
        try:
            _SPLIT_CACHE[cmd] = shlex.split(cmd)
        except ValueError as e:
            raise ParseError("config %s cannot be parsed (%s); fix hardrail.config.json" % (key, e)) from e
    return list(_SPLIT_CACHE[cmd])


def _configured_tests(config):
    cmds = [('defaults.test', config.get('defaults', {}).get('test'))]
    for name, pkg in config.get('packages', {}).items():
        if isinstance(pkg, dict):
            cmds.append(('packages.%s.test' % name, pkg.get('test')))
    return [_split_cached(c, key) for key, c in cmds if isinstance(c, str) and c.strip()]


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
        return _direct(skip_manager_opts(argv[1:], base))
    if base in _PKG_MANAGERS:
        rest = skip_manager_opts(argv[1:], base)
        if not rest:
            return None
        hit = _direct(skip_manager_opts(rest[1:]) if rest[0] in ('exec', 'dlx') else rest)
        if hit:
            return hit
        if base == 'yarn' and rest[0] == 'workspace' and len(rest) > 2:
            rest = skip_manager_opts(rest[2:]) or ['']
        elif base == 'yarn' and rest[:2] == ['workspaces', 'foreach']:
            rest = skip_manager_opts(rest[2:]) or ['']
        if base == 'npm' and rest[0] in ('t', 'tst'):
            rest = ['test'] + rest[1:]
        if rest[0] in ('run', 'run-script') and len(rest) > 1:
            rest = skip_manager_opts(rest[1:], base) or ['']
        if _is_script(rest[0]):
            return rest[0], rest[1:]
    if base == 'turbo':
        rest = argv[2:] if argv[1:2] == ['run'] else argv[1:]
        if rest[:1] == ['test']:
            return 'test', rest[1:]
    if base == 'nx' and argv[1:2] == ['test']:
        return 'test', argv[2:]
    if base == 'nx' and argv[1:2] == ['run-many']:
        hit = _nx_target(argv[2:])
        if hit:
            return hit
    for cmd in _configured_tests(config):
        if argv[:len(cmd)] == cmd:
            return 'configured', argv[len(cmd):]
    return None


def _nx_target(args):
    """('test', other args) when `nx run-many -t|--target[=] <tasks>` includes test."""
    for k, a in enumerate(args):
        name, eq, val = a.partition('=')
        if name in ('-t', '--target', '--targets'):
            if not eq:
                val = args[k + 1] if k + 1 < len(args) else ''
            if 'test' in val.split(','):
                return 'test', args[:k] + args[k + (1 if eq else 2):]
    return None


def _paths(args, cwd, vitest):
    """The words in runner/script args that name a test path: not flags, not flag values,
    and either path-shaped, a jest path-pattern value, or an existing file/dir under cwd."""
    out = []
    i = 0
    leading = vitest  # only vitest has subcommands
    while i < len(args):
        a = args[i]
        i += 1
        if a.startswith('-'):
            name, eq, val = a.partition('=')
            if name in _JEST_PATH_FLAGS:
                if not eq and i < len(args):
                    val, i = args[i], i + 1
                if val not in _ANY_PATTERN:
                    out.append(val)
            elif a in _RUNNER_VALUE_FLAGS:
                i += 1
            continue
        if leading and a in _VITEST_LEADING:  # a subcommand, never a path
            leading = False
            continue
        leading = False
        if _is_scope(a) and ('/' in a or _PATH_ARG.search(a) or os.path.exists(os.path.join(cwd, a))):
            out.append(a)
    return out


def _vitest_sub(args):
    """The leading vitest subcommand in args, or None."""
    first = next((a for a in args if not a.startswith('-')), None)
    return first if first in _VITEST_LEADING else None


def _fix_cmd(argv, name, args, path, config):
    """The scoped one-shot command: the runner as invoked, or the configured test command."""
    if name not in _RUNNERS:
        return f"{config['defaults']['test']} {path}"
    k = next(i for i, a in enumerate(argv) if os.path.basename(a) == name)
    out = argv[:k + 1]
    if name == 'vitest':
        sub = _vitest_sub(args)
        if sub in _VITEST_SUBCMDS:
            out.append(sub)
        out.append('--run')
    elif _explicit_watch(args):
        out.append('--watchAll=false')
    return ' '.join(out + [path])


def _result(action, rule, msg, config):
    if action == "block" and config.get("guard", {}).get("mode") == "warn":
        action = "warn"
    return Decision(action, rule, msg)


def _unparsable(config, why=None):
    return _result("block", "unparsable", why or "cannot parse command; split it or quote it plainly", config)


def _find_exec_cmds(c):
    """The commands `find ... -exec|-execdir|-ok|-okdir <cmd> ... ;|+` runs."""
    if c[:1] != ['find']:
        return []
    out = []
    for k, w in enumerate(c):
        if w in _FIND_EXEC:
            end = next((m for m in range(k + 1, len(c)) if c[m] in (';', '+')), len(c))
            out.append(c[k + 1:end])
    return out


def _push_forms(s):
    """Every form of a simple that may run git: as written, with a package manager's
    exec / dlx / npx dropped, and each `find -exec` command; wrappers stripped from each."""
    c = _cmd(s)
    yield c
    for f in [_exec_form(c)] + _find_exec_cmds(c):
        f = _strip_git(strip_env_and_wrappers(f)[1]) if f else None
        if f:
            yield [os.path.basename(f[0])] + f[1:]


def _exec_form(c):
    """The command `npx` / `bunx` / `<manager> exec|dlx|x` runs, else None."""
    base = c[0] if c else ''
    if base in ('npx', 'bunx'):
        return skip_manager_opts(c[1:], base)
    if base in _PKG_MANAGERS:
        rest = skip_manager_opts(c[1:], base)
        if rest[:1] in (['exec'], ['dlx'], ['x']):
            return skip_manager_opts(rest[1:], base)
    return None


def rule_force_push(simples, config, cwd="."):
    for s in simples:
        for c in _push_forms(s):
            if c[:2] != ['git', 'push']:
                continue
            for a in c[2:]:
                if a in ('--force', '--mirror') or _SHORT_F.match(a) or (a.startswith('+') and len(a) > 1):
                    return _result("block", "force_push",
                                   "force push is blocked; use git push --force-with-lease", config)
                if a == '--delete' or _SHORT_D.match(a) or (a.startswith(':') and len(a) > 1):
                    return _result("block", "force_push", _DELETE_MSG, config)
                if _SUBST_PH in a or '$' in a:
                    return _unparsable(config, _EXPANSION_MSG)
    return None


def _gated_forms(c):
    """The command as written, and with manager options / run / exec / dlx dropped:
    `pnpm --filter x add zod` -> also `pnpm add zod` and `add zod`. A `run` target is a
    script name, so it is not matched on its own: `pnpm run rm` is not `rm`."""
    if not c:
        return [c]
    form = _heavy_form(c).split()
    is_script = os.path.basename(c[0]) in _PKG_MANAGERS and skip_manager_opts(c[1:], c[0])[:1] == ['run']
    return [c, [c[0]] + form] if is_script else [c, [c[0]] + form, form]


def rule_xargs_stdin(simples, config, cwd="."):
    for s in simples:
        if is_pipe(s.sep) and xargs_writes_command(s.argv):
            return _unparsable(config, XARGS_ERR)
    return None


def _is_gated(s, config):
    forms = _gated_forms(_cmd(s))  # argv[0] compared by basename
    for prefix in config["guard"]["gatedActions"]:
        p = prefix.split()
        if p and any(f[:len(p)] == p for f in forms):
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
    if len(simples) > 1:
        gated = next((s for s in simples if s.payload_of and _is_gated(s, config)), None)
        if gated is not None:
            return _result("block", "chained_gated",
                           f"gated action inside a shell payload; run `{gated.raw}` directly as its own call",
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


def _docker_logs_fix(c):
    """`docker [opts] [compose [opts]] logs -f ...` -> the same without follow, with a tail."""
    i = scan_opts(c, 1, _DOCKER_GLOBAL_VALUE)[0]
    if i < len(c) and c[i] == 'compose':
        i += 1
    i = scan_opts(c, i, _COMPOSE_VALUE)[0]
    if c[i:i + 1] != ['logs']:
        return None
    args = c[i + 1:]
    if not any(a == '--follow' or _SHORT_F.match(a) for a in args):
        return None
    rest = [a if a in ('-f', '--follow') or not _SHORT_F.match(a) else a.replace('f', '') for a in args]
    rest = _without(rest, ('-f', '--follow'))
    tail = [] if any(a.startswith('--tail') or a == '-n' for a in rest) else ['--tail=200']
    return "use " + ' '.join(c[:i + 1] + tail + rest)


def _tail_fix(c):
    """`tail -f/-F/--follow file...` -> `tail -n 200 file...`."""
    follow, files, i = False, [], 1
    while i < len(c):
        a = c[i]
        i += 1
        if a == '--':
            files += ['--'] + c[i:] if any(w.startswith('-') for w in c[i:]) else c[i:]
            break
        if a == '--follow' or a.startswith('--follow='):
            follow = True
        elif a in _TAIL_VALUE:
            i += 1
        elif a.startswith('-') and not a.startswith('--') and len(a) > 1:
            follow = follow or 'f' in a[1:] or 'F' in a[1:]
            if a[-1] in 'ncs':  # `-fn 20`: the cluster ends in a value option
                i += 1
        elif not a.startswith('-'):
            files.append(a)
    return "use tail -n 200 " + (' '.join(files) or 'FILE') if follow else None


def _watch_fix(s, config, cwd):
    """The one-shot command for a watch-mode simple, or None when it does not watch."""
    env, argv = strip_env_and_wrappers(s.argv)
    c = _strip_git(argv)
    base = os.path.basename(c[0]) if c else ''
    if base in _DOCKER_BASES:
        fix = _docker_logs_fix([base] + c[1:])
        if fix:
            return fix
    if base == 'tail':
        return _tail_fix(c)
    if any(e.startswith('CI=') for e in env) and not _explicit_watch(argv):
        return None
    if any(os.path.basename(a) in ('playwright', 'test:e2e') for a in c) and any(a in _E2E_WATCH for a in c):
        return "run it without the flag: " + ' '.join(_without(argv, _E2E_WATCH))
    hit = _runner(argv, config)
    if not hit:
        return None
    name, args = hit
    explicit = _explicit_watch(args) or (name == 'vitest' and _vitest_sub(args) in ('watch', 'dev'))
    paths = _paths(args, cwd, name != 'jest')
    fix = _fix_cmd(argv, name, args, paths[0] if paths else _PLACEHOLDER, config)
    if name == 'jest':
        return "run once: " + fix if explicit else None
    if name == 'vitest':
        if not explicit and any(a in _RUN_FLAGS for a in args):
            return None
        return "run once: " + fix
    if name.startswith('test:') and name.endswith('watch'):
        return "run the package's `test` script with a path: " + fix
    if explicit:
        return "drop --watch and run once: " + fix
    return None


def rule_watch_mode(simples, config, cwd="."):
    for s in simples:
        fix = _watch_fix(s, config, cwd)
        if fix:
            return _result("block", "watch_mode", f"watch mode never exits; {fix}", config)
    return None


def _unscoped_decision(s, prev, name, args,
                       config, cwd):
    if '--help' in args:
        return None
    argv = strip_env_and_wrappers(s.argv)[1]
    root = find_root(cwd) or find_workspace_root(cwd) or cwd
    pkg = resolve_package(s, cwd, config, root, prev=prev)
    base = config['defaults']['test']
    script = pkg.test_script.split() if isinstance(pkg, Package) and pkg.test_script else []
    script_runner = os.path.basename(script[0]) if script else None
    paths = _paths(args, cwd, name == 'vitest' or (name not in _RUNNERS and script_runner != 'jest'))
    path = paths[0] if paths else _PLACEHOLDER
    if pkg == "all":
        cmd = ('pnpm --filter <name> ' + base[5:]) if base.startswith('pnpm ') else base
        return _result("block", "unscoped_test",
                       f"this runs every workspace package; target one package: {cmd} {path}", config)
    if isinstance(pkg, Package) and name not in _RUNNERS and pkg.test_script \
            and _paths(script[1:], pkg.root, script_runner == 'vitest'):
        rel = os.path.relpath(pkg.root, os.path.abspath(cwd))
        fix = _fix_cmd(argv, name, args, path, config)
        if rel != '.':
            fix = f"pnpm -C {rel} {fix[5:]}" if base.startswith('pnpm ') else f"(in {rel}) {fix}"
        return _result("block", "unscoped_test",
                       f"the package test script already has a path, so extra paths do not scope it; use: {fix}",
                       config)
    if paths:
        return None
    fix = _fix_cmd(argv, name, args, path, config)
    return _result("block", "unscoped_test", f"unscoped test run; scope it: {fix}", config)


def rule_unscoped_test(simples, config, cwd="."):
    for i, s in enumerate(simples):
        hit = _runner(strip_env_and_wrappers(s.argv)[1], config)
        if not hit:
            continue
        prev = [p for p in simples[:i] if p.payload_of == s.payload_of]
        d = _unscoped_decision(s, prev, hit[0], hit[1], config, cwd)
        if d:
            return d
    return None


def _heavy_form(argv):
    if argv and os.path.basename(argv[0]) in _PKG_MANAGERS + ('npx', 'bunx'):
        argv = skip_manager_opts(argv[1:], os.path.basename(argv[0]))
        if argv[:1] in (['run'], ['exec'], ['dlx']):
            argv = argv[1:]
    return ' '.join(argv)


def rule_heavy(simples, config, cwd="."):
    """An entry matches the whole command or a word prefix of it; `turbo run x` reads as `x`."""
    for s in simples:
        c = _cmd(s)
        form = _heavy_form(c[2:] if c[:2] == ['turbo', 'run'] else c)
        if any(_glob_match(g, form) or _glob_match(g + ' *', form) for g in config.get("heavy", [])):
            return Decision("warn", "heavy",
                            f"heavy command; run it outside the agent loop or in a background task: {s.raw}")
    return None


RULES = [rule_force_push, rule_xargs_stdin, rule_chained_gated, rule_watch_mode,
                     rule_unscoped_test, rule_heavy]


def decide(command, cwd, config):
    try:
        simples = split_commands(command)
    except ParseError as e:
        return _unparsable(config, str(e) or None)
    except RecursionError:
        return _unparsable(config, "payload too deep to inspect; run it as a script file")
    except Exception as e:  # never let a parser bug let the command through
        return _unparsable(config, "guard could not evaluate the command (%s)" % type(e).__name__)
    try:
        for rule in RULES:
            d = rule(simples, config, cwd)
            if d:
                return d
    except ParseError as e:
        return _unparsable(config, str(e))
    except Exception as e:
        return _unparsable(config, "guard could not evaluate the command (%s)" % type(e).__name__)
    return ALLOW
