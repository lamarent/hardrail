"""Shared argv helpers: env/wrapper prefixes and package-manager option skipping."""

import os
import re

from core.shell import ParseError, variable_command_word

_ENV = re.compile(r'^[A-Za-z_]\w*=')
WRAPPERS = {  # name -> options that take a value (next word)
    'sudo': ('-u', '-g', '-h', '-p', '-C', '-D', '-r', '-t', '-T', '-U', '--user', '--group'),
    'doas': ('-u', '-C'), 'env': ('-u', '--unset', '-C', '--chdir'),
    'nohup': (), 'command': (), 'exec': ('-a',), 'time': (), 'corepack': (),
    'timeout': ('-s', '--signal', '-k', '--kill-after'), 'nice': ('-n', '--adjustment'),
    'ionice': ('-c', '-n', '-p'), 'stdbuf': ('-i', '-o', '-e'), 'chronic': (),
    'xargs': ('-I', '-n', '-P', '-d', '-L', '-s', '-E', '-a', '--max-args', '--max-procs', '--delimiter'),
}
POSITIONAL_AFTER = {'timeout': 1}   # words consumed after the options (the duration)
MANAGERS = ('pnpm', 'npm', 'yarn', 'bun')
# Manager options that take the next word as their value (`--opt=value` is one word).
VALUE_FLAGS = ('-C', '-F', '--filter', '--filter-prod', '--dir', '--prefix', '--cwd', '--reporter',
               '--workspace-concurrency', '--loglevel', '--config', '-c', '--registry', '--store-dir')
NPM_WORKSPACE_FLAGS = ('-w', '--workspace')     # value flags for npm only


def _is_split_string(name, a):
    if name != 'env':
        return False
    if a.startswith('--'):
        return a.partition('=')[0] == '--split-string'
    return 'S' in a[1:]


def _skip_wrapper_opts(name, argv, i):
    """Index of the first word after the options of the wrapper whose name is argv[i - 1]."""
    values = WRAPPERS[name]
    n = len(argv)
    while i < n and argv[i].startswith('-') and argv[i] != '-':
        a = argv[i]
        i += 1
        if a == '--':
            break
        if _is_split_string(name, a):
            raise ParseError("env -S payload cannot be inspected")
        if a.startswith('--'):
            if a in values:
                i += 1
            continue
        # Short cluster, getopt style: a value letter takes the rest of the word, or the next word.
        for k in range(1, len(a)):
            if '-' + a[k] in values:
                if k == len(a) - 1:
                    i += 1
                break
    for _ in range(POSITIONAL_AFTER.get(name, 0)):
        if i < n:
            i += 1
    return i


def strip_env_and_wrappers(argv):
    """Split leading env assignments and wrappers (sudo, time, env...) and their options
    from the command: (prefix, rest).

    Raises ParseError when the command cannot be named: `env -S` (a split payload), a
    variable command word, or options left in front of the command after a wrapper.
    """
    i = 0
    wrapped = False
    while i < len(argv):
        w = argv[i]
        if _ENV.match(w):
            i += 1
            continue
        name = os.path.basename(w)
        if name not in WRAPPERS:
            break
        wrapped = True
        i = _skip_wrapper_opts(name, argv, i + 1)
    rest = argv[i:]
    if rest and variable_command_word(rest[0]):
        raise ParseError("command word is a variable; write the command literally")
    if wrapped and rest and rest[0].startswith('-'):
        raise ParseError("wrapper options hide the command word")
    return argv[:i], rest


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
