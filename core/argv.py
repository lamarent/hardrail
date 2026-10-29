"""Shared argv helpers: env/wrapper prefixes and package-manager option skipping."""

import os
import re

from core.shell import (_SHELLS, DOAS_VALUE_OPTS, SUDO_VALUE_OPTS, ParseError, scan_opts,
                        variable_command_word)

_ENV = re.compile(r'^[A-Za-z_]\w*=')
WRAPPERS = {  # name -> options that take a value (next word)
    'sudo': SUDO_VALUE_OPTS,
    'doas': DOAS_VALUE_OPTS, 'env': ('-u', '--unset', '-C', '--chdir'),
    'nohup': (), 'command': (), 'exec': ('-a',), 'time': (), 'corepack': (),
    'timeout': ('-s', '--signal', '-k', '--kill-after'), 'nice': ('-n', '--adjustment'),
    'ionice': ('-c', '-n', '-p'), 'stdbuf': ('-i', '-o', '-e'), 'chronic': (),
    'xargs': ('-I', '-n', '-P', '-d', '-L', '-s', '-E', '-a', '--max-args', '--max-procs', '--delimiter'),
}
POSITIONAL_AFTER = {'timeout': 1}   # words consumed after the options (the duration)
# Commands xargs may build from piped input: they only read.
XARGS_READ_ONLY = ('ls', 'cat', 'grep', 'rg', 'wc', 'head', 'tail', 'stat', 'file', 'echo')
XARGS_ERR = "xargs builds the command from stdin; run it as a script file or list the files explicitly"
MANAGERS = ('pnpm', 'npm', 'yarn', 'bun')
# Manager options that take the next word as their value (`--opt=value` is one word).
VALUE_FLAGS = ('-C', '-F', '--filter', '--filter-prod', '--dir', '--prefix', '--cwd', '--reporter',
               '--workspace-concurrency', '--loglevel', '--config', '-c', '--registry', '--store-dir')
NPM_WORKSPACE_FLAGS = ('-w', '--workspace')     # value flags for npm only


def _skip_wrapper_opts(name, argv, i):
    """Index of the first word after the options of the wrapper whose name is argv[i - 1]."""
    i, seen = scan_opts(argv, i, WRAPPERS[name])
    if name == 'env' and ('-S' in seen or '--split-string' in seen):
        raise ParseError("env -S payload cannot be inspected")
    for _ in range(POSITIONAL_AFTER.get(name, 0)):
        if i < len(argv):
            i += 1
    return i


def _strip(argv):
    """(index of the command word, xargs is in the wrapper chain)."""
    i = 0
    xargs = False
    while i < len(argv):
        w = argv[i]
        if _ENV.match(w):
            i += 1
            continue
        name = os.path.basename(w)
        if name not in WRAPPERS:
            break
        xargs = xargs or name == 'xargs'
        i = _skip_wrapper_opts(name, argv, i + 1)
    return i, xargs


def strip_env_and_wrappers(argv):
    """Split leading env assignments and wrappers (sudo, time, env...) and their options
    from the command: (prefix, rest).

    Raises ParseError when the command cannot be named: `env -S` (a split payload), a
    variable command word, options left in front of the command after a wrapper, or an
    xargs whose command is empty or a shell (it runs text read from stdin).
    """
    i, xargs = _strip(argv)
    rest = argv[i:]
    if xargs and (not rest or os.path.basename(rest[0]) in _SHELLS):
        raise ParseError(XARGS_ERR)
    if rest and variable_command_word(rest[0]):
        raise ParseError("command word is a variable; write the command literally")
    if i and rest and rest[0].startswith('-'):
        raise ParseError("wrapper options hide the command word")
    return argv[:i], rest


def xargs_writes_command(argv):
    """True when xargs in the wrapper chain runs a command that is not read-only: fed
    from a pipe, its input becomes arguments (or the command) that cannot be inspected."""
    i, xargs = _strip(argv)
    return xargs and (i >= len(argv) or os.path.basename(argv[i]) not in XARGS_READ_ONLY)


def _value_flags(manager):
    return VALUE_FLAGS + (NPM_WORKSPACE_FLAGS if manager == 'npm' else ())


def skip_manager_opts(args, manager=None):
    """Drop leading manager options (and their values) before the subcommand."""
    values = _value_flags(manager)
    i = 0
    while i < len(args) and args[i].startswith('-') and args[i] != '-':
        a = args[i]
        cluster = len(a) > 2 and a[1] != '-' and a[-1] in 'FC' and '=' not in a
        i += 2 if a in values or cluster else 1
    return args[i:]


def manager_flags(argv):
    """(flag, value) pairs between the package manager and its subcommand.

    Handles `--flag value`, `--flag=value`, `-C=value`, and short clusters (`-rF value`).
    """
    argv = strip_env_and_wrappers(argv)[1]
    if not argv or os.path.basename(argv[0]) not in MANAGERS:
        return []
    values = _value_flags(os.path.basename(argv[0]))
    out = []
    i = 1
    while i < len(argv) and argv[i].startswith('-') and argv[i] != '-':
        t = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else ""
        if t.startswith('--'):
            name, eq, val = t.partition('=')
            if name in values and not eq:
                val, i = nxt, i + 1
            out.append((name, val if (eq or name in values) else None))
        elif '=' in t:
            name, _, val = t.partition('=')
            out.append((name, val))
        elif t in values:
            out.append((t, nxt))
            i += 1
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
