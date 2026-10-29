"""Shared argv helpers: env/wrapper prefixes and package-manager option skipping."""

import os
import re

_ENV = re.compile(r'^[A-Za-z_]\w*=')
WRAPPERS = ('sudo', 'time', 'env', 'nohup', 'command', 'exec', 'corepack')
# Manager options that take the next word as their value (`--opt=value` is one word).
VALUE_FLAGS = ('-C', '-F', '--filter', '--filter-prod', '--dir', '--prefix', '--cwd')


def strip_env_and_wrappers(argv):
    """Split leading env assignments and wrappers (sudo, time, env...) from the command."""
    i = 0
    while i < len(argv) and (_ENV.match(argv[i]) or os.path.basename(argv[i]) in WRAPPERS):
        i += 1
    return argv[:i], argv[i:]


def skip_manager_opts(args):
    """Drop leading manager options (and their values) before the subcommand."""
    i = 0
    while i < len(args) and args[i].startswith('-') and args[i] != '-':
        a = args[i]
        cluster = len(a) > 2 and a[1] != '-' and a[-1] in 'FC' and '=' not in a
        i += 2 if a in VALUE_FLAGS or cluster else 1
    return args[i:]
