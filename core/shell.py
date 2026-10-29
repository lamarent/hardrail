"""Shell tokeniser: split a command line into simple commands and expand payloads."""

import os
import re
import shlex

_SEPS = ';&|\n'
_SENT = {c: chr(0xE000 + i) for i, c in enumerate(_SEPS)}
_UNSENT = {v: k for k, v in _SENT.items()}
SHELLS = ('bash', 'sh', 'zsh', 'dash', 'ksh', 'mksh', 'ash', 'busybox', 'ksh93', 'fish')
_HEREDOC_PH = '_heredoc_'
_HD_STOP = ' \t\r\n;&|<>()'
_C_FLAG = re.compile(r'-[A-Za-z]*c[A-Za-z]*')
_KEYWORDS = ('if', 'then', 'else', 'elif', 'do', 'while', 'until', '!', '{', 'time', 'coproc')
_CLOSERS = ('fi', 'done', 'esac', '}')
_FUNC_DEF = re.compile(r'^\w+\(\)\{?$')
_STDIN_SCRIPT = ('-', '/dev/stdin', '/dev/fd/0')
# Options that take a value (next word), for wrappers whose options the parser walks.
SUDO_VALUE_OPTS = ('-u', '-g', '-h', '-p', '-C', '-D', '-r', '-t', '-T', '-U', '-R',
                   '--user', '--group', '--host', '--prompt', '--close-from', '--chdir',
                   '--role', '--type', '--command-timeout', '--other-user', '--chroot')
DOAS_VALUE_OPTS = ('-u', '-C')
# Wrapper -> (value options, options that run the command or stdin through a shell).
_SHELL_WRAPPERS = {
    'sudo': (SUDO_VALUE_OPTS, ('-s', '-i', '--shell', '--login')),
    'doas': (DOAS_VALUE_OPTS, ('-s',)),
}
_ASSIGN = re.compile(r'^[A-Za-z_]\w*=')
_SOURCE = ('source', '.')
# Wrappers whose options are modelled in full: name -> (value options, flag options).
# Any other option raises ParseError, since it could hide the command word.
WRAPPER_OPTS = {
    'flock': (('-w', '--wait', '--timeout', '-E', '--conflict-exit-code', '-c', '--command'),
              ('-s', '--shared', '-x', '-e', '--exclusive', '-u', '--unlock', '-n', '--nb',
               '--nonblock', '-o', '--close', '-F', '--no-fork', '--verbose')),
    'unbuffer': ((), ('-p',)),
    'caffeinate': (('-t', '-w'), ('-d', '-i', '-m', '-s', '-u')),
    'script': (('-c', '--command', '-t', '-T', '--log-timing', '-B', '--log-io', '-I', '--log-in',
                '-O', '--log-out', '-E', '--echo', '-m', '--logging-format'),
               ('-a', '--append', '-e', '--return', '-f', '--flush', '-q', '--quiet', '-k', '-F',
                '-r', '-p')),
    'watch': (('-n', '--interval', '-q', '--equexit'),
              ('-b', '--beep', '-c', '--color', '-C', '--no-color', '-d', '--differences', '-e',
               '--errexit', '-g', '--chgexit', '-p', '--precise', '-t', '--no-title', '-w',
               '--no-wrap', '-x', '--exec', '-r', '--no-rerun')),
    'ssh': (('-B', '-b', '-c', '-D', '-E', '-e', '-F', '-I', '-i', '-J', '-L', '-l', '-m', '-O',
             '-o', '-P', '-p', '-Q', '-R', '-S', '-W', '-w'),
            ('-4', '-6', '-A', '-a', '-C', '-f', '-G', '-g', '-K', '-k', '-M', '-N', '-n', '-q',
             '-s', '-T', '-t', '-V', '-v', '-X', '-x', '-Y', '-y')),
    'parallel': (('-j', '--jobs', '-N', '-n', '--max-args', '-a', '--arg-file', '-I', '-d',
                  '--delimiter', '-S', '--sshlogin', '--joblog', '--results', '--timeout', '--halt',
                  '--retries', '--tmpdir', '-C', '--colsep'),
                 ('-k', '--keep-order', '-v', '--verbose', '-q', '--quote', '--dry-run', '--tag',
                  '--line-buffer', '--lb', '--progress', '--bar', '--eta', '-0', '--null', '-u',
                  '--ungroup', '-X', '-m', '--xapply', '--link', '--pipe', '--group',
                  '--will-cite', '--no-notice', '-r', '--no-run-if-empty')),
}
_PARALLEL_SEPS = (':::', '::::', ':::+', '::::+')
_REDIR = re.compile(r'^\d*[<>]{1,2}&?\S*$')
_BARE_REDIR = ('<', '>', '>>', '2>', '&>', '&>>')
_MAX_INPUT = 65536
_PIPE_ERR = "command piped into a shell cannot be inspected; run it as a script file"


class Simple:
    # payload_of: None | "sh -c" | "eval" | "heredoc" | "subst" | "pipe"
    # sep: separator token before this simple at its level ('' for the first); not compared.
    __slots__ = ("argv", "raw", "payload_of", "sep")

    def __init__(self, argv, raw, payload_of=None, sep=''):
        self.argv = argv
        self.raw = raw
        self.payload_of = payload_of
        self.sep = sep

    def __eq__(self, other):
        return isinstance(other, Simple) and (self.argv, self.raw, self.payload_of) == \
            (other.argv, other.raw, other.payload_of)

    __hash__ = None

    def __repr__(self):
        return "Simple(argv=%r, raw=%r, payload_of=%r)" % (self.argv, self.raw, self.payload_of)


class ParseError(Exception):
    pass


_MAX_BODY_LINES = 200
_MAX_BODY_BYTES = 8192
_TOO_LARGE = "payload too large to inspect; run it as a script file"
SUBST_PH = '_subst_'
_MAX_SUBST_DEPTH = 20
_TOO_DEEP = "payload too deep or too large to inspect; run it as a script file"
_CASE = re.compile(r'\bcase\s')
_BQ_ESC = re.compile(r'\\([`\\$])')  # inside `...`, \` \\ \$ lose the backslash


def _check_size(text):
    if text.count('\n') >= _MAX_BODY_LINES or len(text.encode('utf-8', 'replace')) > _MAX_BODY_BYTES:
        raise ParseError(_TOO_LARGE)


def _s(c):
    return _SENT.get(c, c)


def _protect(cmd):
    """Replace quoted, escaped or redirect separator chars with sentinels.

    Returns the new text and, for each output char, its index in `cmd`.
    """
    out = []
    idx = []

    def put(ch, src):
        out.append(ch)
        idx.append(src)

    q = None
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if q == "'":
            if c == "'":
                q = None
        elif q == '"':
            if c == '\\' and i + 1 < n:
                put(c, i)
                put(_s(cmd[i + 1]), i + 1)
                i += 2
                continue
            if c == '"':
                q = None
        else:
            if c == '$' and i + 1 < n and cmd[i + 1] in '\'"':
                raise ParseError("$'...' quoting is not supported; write the string plainly")
            if c == '\\' and i + 1 < n:
                nxt = cmd[i + 1]
                if nxt == '\n':
                    i += 2
                    continue
                if nxt in _SEPS:
                    put(_SENT[nxt], i + 1)
                else:
                    put(c, i)
                    put(nxt, i + 1)
                i += 2
                continue
            if c in '\'"':
                q = c
            elif c == '#' and (i == 0 or cmd[i - 1] in ' \t\r\n;&|('):
                while i < n and cmd[i] != '\n':
                    i += 1
                continue
            elif c == '&' and ((i > 0 and cmd[i - 1] in '<>') or (i + 1 < n and cmd[i + 1] == '>')):
                put(_SENT['&'], i)
                i += 1
                continue
            else:
                put(c, i)
                i += 1
                continue
        put(_s(c), i)
        i += 1
    return ''.join(out), idx


def _restore(word):
    return ''.join(_UNSENT.get(c, c) for c in word)


def _spans(p):
    """Token spans (start, end, is_separator) of protected text, mirroring shlex."""
    res = []
    i, n = 0, len(p)
    while i < n:
        c = p[i]
        if c in ' \t\r':
            i += 1
        elif c in _SEPS:
            j = i
            while j < n and p[j] in _SEPS:
                j += 1
            res.append((i, j, True))
            i = j
        else:
            j, q = i, None
            while j < n:
                ch = p[j]
                if q:
                    if q == '"' and ch == '\\' and j + 1 < n:
                        j += 2
                        continue
                    if ch == q:
                        q = None
                elif ch == '\\' and j + 1 < n:
                    j += 2
                    continue
                elif ch in '\'"':
                    q = ch
                elif ch in ' \t\r' or ch in _SEPS:
                    break
                j += 1
            res.append((i, j, False))
            i = j
    return res


def _unwrap(argv):
    """Drop reserved words, group braces, function heads, subshell parens and `case`
    patterns so the command words come first.

    Returns (argv, closed): `closed` is True when a group closer (`)`, `}`, `done`,
    `fi`, `esac`) was removed, i.e. the words end a compound command.
    """
    closed = False
    while argv:
        w = argv[0]
        if w in _CLOSERS:
            closed = True
            argv = argv[1:]
        elif w in _KEYWORDS or w.endswith('{') or _FUNC_DEF.match(w):
            argv = argv[1:]
            if w == 'time' and argv[:1] == ['-p']:
                argv = argv[1:]
            if w == 'time' and argv[:1] == ['--']:
                argv = argv[1:]
        elif w == 'function' and len(argv) > 1:
            argv = argv[2:]
        elif w.startswith('('):
            argv = [w[1:]] + argv[1:] if w != '(' else argv[1:]
        else:
            break
    while argv:
        w = argv[-1]
        if w in _CLOSERS:
            argv = argv[:-1]
        elif w.endswith(')') and '(' not in w:
            argv = argv[:-1] + ([w[:-1]] if w[:-1] else [])
        else:
            break
        closed = True
    if argv and argv[0] == 'case' and 'in' in argv:
        k = argv.index('in')
        j = next((m for m in range(k + 1, len(argv)) if argv[m].endswith(')')), None)
        argv = argv[j + 1:] if j is not None else []
    elif argv and len(argv[0]) > 1 and argv[0].endswith(')') and '(' not in argv[0]:
        argv = argv[1:]
    return argv, closed


def _split_plain(text):
    prot, idx = _protect(text)
    lex = shlex.shlex(prot, posix=True, punctuation_chars=_SEPS)
    lex.whitespace = ' \t\r'
    lex.whitespace_split = True
    lex.commenters = ''
    try:
        tokens = list(lex)
    except ValueError as e:
        raise ParseError("cannot parse command (%s); split it or quote it plainly" % e) from e
    spans = _spans(prot)
    use_spans = len(spans) == len(tokens)
    result = []
    cur = []
    first = last = 0
    sep = ''
    closed = False  # the previous group ended a compound command

    def flush():
        nonlocal closed
        if use_spans:
            raw = text[idx[first]:idx[last - 1] + 1]
        else:
            raw = ' '.join(cur)
        argv, was_closed = _unwrap(list(cur))
        sp = sep[:2]
        if argv:
            # A compound command's output piped into a shell cannot be traced to one simple.
            if closed and is_pipe(sp) and _is_shell_reader(argv):
                raise ParseError(_PIPE_ERR)
            result.append(Simple(argv=argv, raw=raw, sep=sp))
        closed = was_closed
        cur.clear()

    for k, tok in enumerate(tokens):
        if tok and all(c in _SEPS for c in tok):
            if cur:
                flush()
                sep = tok
            else:
                sep += tok
        else:
            if not cur and use_spans:
                first = spans[k][0]
            if use_spans:
                last = spans[k][1]
            cur.append(_restore(tok))
    if cur:
        flush()
    return result


def is_pipe(sep):
    """True for `|`, `|&` or a pipe followed by a newline (not `||`)."""
    return sep[:1] == '|' and sep[1:2] != '|'


def _drop_redirects(argv):
    """argv without redirect words (`2>/dev/null`, `>&2`, `&>f`) and the target after a
    bare `<`, `>`, `>>`, `2>`, `&>`. Here-strings (`<<<`) are kept."""
    out = []
    skip = False
    for w in argv:
        if skip:
            skip = False
        elif w in _BARE_REDIR:
            skip = True
        elif not w.startswith('<<<') and (_REDIR.match(w) or w.startswith('&>')):
            pass
        else:
            out.append(w)
    return out


def _clean(argv):
    """Redirect-free argv for shell detection.

    A shell whose argv carries a substitution word (`bash <(...)`, `bash < <(...)`,
    `sh -c "$(...)"`) runs text that cannot be inspected: ParseError.
    """
    if SUBST_PH in argv:
        k = argv.index(SUBST_PH)
        b = _builtin_index(argv)
        if any(os.path.basename(w) in SHELLS for w in argv[:k]) or \
                (b is not None and b < k and argv[b] in _SOURCE):
            raise ParseError("script from a process substitution cannot be inspected; "
                             "run it as a script file")
    return _drop_redirects(argv)


def _shell_scan(argv, j):
    """Scan the options after the shell word at argv[j].

    Returns ('c', payload_index) for a -c flag (the payload is the first non-option word,
    or the word after `--`), ('flags', index) if only options follow or -s (read from
    stdin) is given, else None. A script word `-`, /dev/stdin or /dev/fd/0 also reads
    stdin. One value word is skipped per `o` / `O` letter in a short-option cluster
    (`-oc pipefail`, `-O extglob`), and after --rcfile / --init-file.
    """
    n = len(argv)
    k = j + 1
    want_c = want_s = False
    while k < n:
        w = argv[k]
        if w == '--':
            k += 1
            break
        if w in ('--rcfile', '--init-file'):
            k += 2
            continue
        if w.startswith('--'):
            k += 1
            continue
        if len(w) < 2 or w[0] not in '-+':
            break
        if w[0] == '-' and _C_FLAG.fullmatch(w):
            want_c = True
        elif w[0] == '-' and 's' in w[1:]:
            want_s = True
        k += 1 + w.count('o') + w.count('O')
    if want_c:
        return ('c', k) if k < n else None
    if want_s or k >= n or argv[k] in _STDIN_SCRIPT:
        return 'flags', k
    return None


def scan_opts(argv, i, values):
    """Getopt-style walk over the options starting at argv[i].

    Returns (index of the first word after the options, options seen). A short value
    option takes the rest of its word or the next word (`-uroot`, `-Eu bob`); `--opt val`
    takes the next word for a value option, `--opt=val` is one word; `--` ends options.
    Options are reported as `-x` per short letter and `--name` for long ones.
    """
    seen = []
    n = len(argv)
    while i < n and argv[i].startswith('-') and argv[i] != '-':
        a = argv[i]
        i += 1
        if a == '--':
            break
        if a.startswith('--'):
            seen.append(a.partition('=')[0])
            if a in values:
                i += 1
            continue
        for k in range(1, len(a)):
            seen.append('-' + a[k])
            if '-' + a[k] in values:
                if k == len(a) - 1:
                    i += 1
                break
    return i, seen


def _payload_of(argv):
    """Return (text, kind) for the first sh -c / eval payload in argv, else None.

    A command word built from a variable (`$cmd`, `"$@"`, `g${x}`, `/usr/bin/$g`) raises
    ParseError; a path whose last segment is literal (`$HOME/bin/tool`) is allowed.
    `su -c <text>` and `sudo -s|-i <words>` run their text through a shell: 'sh -c'.
    """
    argv = _clean(argv)
    k = 0
    while k < len(argv) and _ASSIGN.match(argv[k]):
        k += 1
    if k < len(argv) and variable_command_word(argv[k]):
        raise ParseError("command word is a variable; write the command literally")
    b = _builtin_index(argv)
    if b is not None and argv[b:b + 1] == ['eval'] and len(argv) > b + 1:
        return ' '.join(argv[b + 1:]), 'eval'
    for j, w in enumerate(argv):
        if w.startswith('<<<'):
            text = w[3:] if len(w) > 3 else (argv[j + 1] if j + 1 < len(argv) else '')
            if _is_shell_reader(argv[:j] + argv[j + (1 if len(w) > 3 else 2):]):
                _check_size(text)
                return text, 'heredoc'
    for j, w in enumerate(argv):
        if os.path.basename(w) in SHELLS:
            r = _shell_scan(argv, j)
            if r and r[0] == 'c':
                return argv[r[1]], 'sh -c'
    for j, w in enumerate(argv):
        name = os.path.basename(w)
        text = _su_command(argv[j + 1:]) if name == 'su' else \
            _sudo_shell_command(name, argv[j + 1:]) if name in _SHELL_WRAPPERS else None
        if text is not None:
            _check_size(text)
            return text, 'sh -c'
    text = _runner_text(argv)
    if text is not None:
        _check_size(text)
        return text, 'sh -c'
    return None


def scan_known(name, argv, i):
    """scan_opts for a wrapper in WRAPPER_OPTS: index after its options; an option the
    table does not model raises ParseError."""
    values, flags = WRAPPER_OPTS[name]
    i, seen = scan_opts(argv, i, values)
    bad = [o for o in seen if o not in values and o not in flags]
    if bad:
        raise ParseError("%s option %s is not modelled, so the command it runs cannot be found; "
                         "write the command without it" % (name, bad[0]))
    return i


def wrapper_c_payload(name, rest):
    """The text `flock` / `script` run through a shell with -c / --command, else None."""
    k = scan_known(name, rest, 0)
    after = rest[k + 1:k + 3]  # -c may also follow the lock / log file
    for words in (rest[:k], after):
        for m, w in enumerate(words):
            if w.startswith('--command='):
                return w.partition('=')[2]
            if w == '--command' or (w[:1] == '-' and w[1:2] != '-' and w.endswith('c')):
                return words[m + 1] if m + 1 < len(words) else ''
            if words is after:
                break
    return None


def _runner_text(argv):
    """Text a command-running tool hands to a shell: `watch`, `ssh host`, `parallel`, and
    `flock` / `script` with -c. Only the command word (after env and wrappers) counts."""
    from core.argv import strip_env_and_wrappers  # not at module level: core.argv imports core.shell (cycle)
    try:
        rest = strip_env_and_wrappers(argv)[1]
    except ParseError:
        return None  # the rules report it
    name = os.path.basename(rest[0]) if rest else ''
    if name in ('flock', 'script'):
        return wrapper_c_payload(name, rest[1:])
    if name == 'watch':
        k = scan_known(name, rest, 1)
        return ' '.join(rest[k:]) or None
    if name == 'ssh':
        k = scan_known(name, rest, 1)
        k = scan_known(name, rest, k + 1) if k < len(rest) else k  # options may follow the host
        return ' '.join(rest[k:]) or None
    if name == 'parallel':
        words = rest[scan_known(name, rest, 1):]
        cut = next((m for m, w in enumerate(words) if w in _PARALLEL_SEPS), len(words))
        if not words[:cut]:
            raise ParseError("parallel runs commands read from its input; run it as a script file")
        return ' '.join(words[:cut])
    return None


def _builtin_index(argv):
    """Index of the command word after assignments and `builtin` / `command`; None for
    `command -v|-V` (a lookup, nothing runs)."""
    k = 0
    while k < len(argv) and _ASSIGN.match(argv[k]):
        k += 1
    while k < len(argv) and argv[k] in ('builtin', 'command'):
        k += 1
        while k < len(argv) and argv[k] in ('-p', '--'):
            k += 1
        if k < len(argv) and argv[k] in ('-v', '-V'):
            return None
    return k


def _source_reader(argv):
    """`source` / `.` of stdin (`-`, /dev/stdin, /dev/fd/0): it runs what it reads."""
    b = _builtin_index(argv)
    if b is None or argv[b:b + 1] not in (['source'], ['.']):
        return False
    rest = argv[b + 1:]
    if rest[:1] == ['--']:
        rest = rest[1:]
    return rest[:1] != [] and rest[0] in _STDIN_SCRIPT


def variable_command_word(word):
    """True when the command a word names comes from a variable: `$x`, `g${x}`,
    `$d/$g`, `/usr/bin/$g`. A literal last path segment (`$HOME/bin/tool`) is a name."""
    if '$' not in word:
        return False
    last = word.rsplit('/', 1)[-1]
    return not last or any(ch in last for ch in '${}')


_SU_VALUE_LETTERS = 'sgGw'


def _su_command(rest):
    """The text of `su ... -c <text>` / `--command[=]<text>`, else None."""
    k = 0
    while k < len(rest):
        w = rest[k]
        k += 1
        if w in ('--command', '--session-command'):
            return rest[k] if k < len(rest) else None
        if w.startswith(('--command=', '--session-command=')):
            return w.partition('=')[2]
        if len(w) < 2 or w[0] != '-' or w[1] == '-':
            continue
        for m in range(1, len(w)):
            if w[m] == 'c':
                if m < len(w) - 1:
                    return w[m + 1:]
                return rest[k] if k < len(rest) else None
            if w[m] in _SU_VALUE_LETTERS:
                k += 1 if m == len(w) - 1 else 0
                break
    return None


def _shell_wrapper_opts(name, rest):
    """(index after the options, runs a shell) for `sudo` / `doas` options in rest."""
    values, shell_flags = _SHELL_WRAPPERS[name]
    k, seen = scan_opts(rest, 0, values)
    return k, any(f in shell_flags for f in seen)


def _sudo_shell_command(name, rest):
    """The words `sudo -s|-i|--shell|--login <words>` hands to the shell, joined; else None."""
    k, shell = _shell_wrapper_opts(name, rest)
    return ' '.join(rest[k:]) if shell and k < len(rest) else None


def _su_reader(argv):
    """`sudo -s|-i`, `doas -s` or `su [user]` with no command: an interactive shell on stdin."""
    name = os.path.basename(argv[0])
    rest = argv[1:]
    if name == 'su':
        rest = [w for w in rest if not w.startswith('-')]
        return len(rest) <= 1
    if name not in _SHELL_WRAPPERS:
        return False
    k, shell = _shell_wrapper_opts(name, rest)
    return shell and k >= len(rest)


def _is_shell_reader(argv):
    argv = _clean(argv)
    if argv and (_su_reader(argv) or _source_reader(argv)):
        return True
    for j, w in enumerate(argv):
        if os.path.basename(w) in SHELLS:
            r = _shell_scan(argv, j)
            if r and r[0] == 'flags':
                return True
    return False


_ECHO_OPT = re.compile(r'^-[neE]+$')


def _pipe_payload(argv):
    """Text that `echo` / `printf` with literal arguments writes into a shell, else
    ParseError: the shell would run output that cannot be inspected."""
    words = _drop_redirects(argv)
    if not words or words[0] not in ('echo', 'printf') or SUBST_PH in argv:
        raise ParseError(_PIPE_ERR)
    cmd, words = words[0], words[1:]
    if cmd == 'echo':
        while words and _ECHO_OPT.match(words[0]):
            words = words[1:]
    elif words and words[0].startswith('-'):
        raise ParseError(_PIPE_ERR)
    # Escapes and printf formats change the text; quoting them is not attempted.
    if any('\\' in w or (cmd == 'printf' and '%' in w) for w in words):
        raise ParseError(_PIPE_ERR)
    text = ' '.join(words)
    _check_size(text)
    return text


def _in_arith(text, i):
    """True when index i sits inside an unclosed `((` on its line (`<<` is a shift there)."""
    s = text[text.rfind('\n', 0, i) + 1:i]
    return s.count('((') > s.count('))')


def _heredoc_at(text, i):
    """Parse the heredoc word after `<<` / `<<-` at text[i].

    Returns (delimiter, quoted, end) with quotes and backslashes removed, or None for an
    empty word. A `$` or backtick in the word, a backslash inside `"..."`, or a
    backslash-newline raises ParseError (fail closed).
    """
    n = len(text)
    j = i + 2
    if text.startswith('-', j):
        j += 1
    while j < n and text[j] in ' \t':
        j += 1
    start = j
    word = []
    quoted = False
    while j < n:
        c = text[j]
        if c in _HD_STOP:
            break
        if c in '$`':
            raise ParseError("heredoc delimiter with $ or ` is not supported")
        if c in '\'"':
            k = j + 1
            while k < n and text[k] != c:
                k += 2 if c == '"' and text[k] == '\\' else 1
            if k >= n:
                raise ParseError("unbalanced quote in heredoc delimiter")
            part = text[j + 1:k]
            if '$' in part or '`' in part:
                raise ParseError("heredoc delimiter with $ or ` is not supported")
            if c == '"' and '\\' in part:
                raise ParseError("backslash in a double-quoted heredoc delimiter is not supported")
            word.append(part)
            quoted = True
            j = k + 1
        elif c == '\\':
            if j + 1 >= n or text[j + 1] == '\n':
                raise ParseError("line continuation in a heredoc delimiter is not supported")
            word.append(text[j + 1])
            quoted = True
            j += 2
        else:
            word.append(c)
            j += 1
    if j == start:
        return None
    return ''.join(word), quoted, j


def _find_heredoc(line, q):
    """First `<<` outside quotes, comments and escapes.

    Returns ((start, delimiter, quoted, end) or None, quote_state).
    """
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if q == "'":
            if c == "'":
                q = None
        elif q == '"':
            if c == '\\':
                i += 2
                continue
            if c == '"':
                q = None
        else:
            if c == '\\':
                i += 2
                continue
            if c in '\'"':
                q = c
            elif c == '#' and (i == 0 or line[i - 1] in ' \t;&|('):
                return None, q
            elif line.startswith('<<', i) and not line.startswith('<<<', i) and (i == 0 or line[i - 1] != '<'):
                h = None if _in_arith(line, i) else _heredoc_at(line, i)
                if h:
                    return (i,) + h, q
        i += 1
    return None, q


def _pipes_to_shell(after):
    """True when a later stage of the pipeline in `after` is a shell reading stdin."""
    prot = _protect(after)[0].replace('|&', '| ')
    end = len(prot)
    for k, ch in enumerate(prot):
        if ch in ';&\n' or (ch == '|' and prot[k + 1:k + 2] == '|'):
            end = k
            break
    for stage in prot[:end].split('|')[1:]:
        simples = _split_plain(_restore(stage))
        if simples and _is_shell_reader(simples[0].argv):
            return True
    return False


def _skip_bodies(cmd, i, pending, n):
    """cmd[i] is a newline after heredoc operators; skip their bodies.

    Returns (index of the newline that ends the last delimiter line (or n),
    [(body_start, body_end, quoted)]).
    """
    spans = []
    for delim, quoted in pending:
        b0, b1 = i + 1, n
        while i < n:
            j = cmd.find('\n', i + 1, n)
            j = n if j < 0 else j
            line = cmd[i + 1:j]
            if line.strip() == delim:
                b1 = max(i, b0)
                i = j
                break
            i = j
        spans.append((b0, b1, quoted))
    del pending[:]
    return i, spans


def _walk(cmd, i, close, level=0, dq=False, end=None):
    """Scan cmd from i for command substitutions.

    `close` is None at the top level, else ')' or '`' for the substitution being scanned.
    Returns (index after the closing char, [(start, body_start, body_end, end)]) for the
    substitutions found directly at this level (nested ones are found when the body is split).
    Process substitutions `<(...)` / `>(...)` count too. Single quotes and heredoc bodies
    are skipped; `$((` arithmetic is not a command, but substitutions inside it are returned.
    A body containing `case` runs to the last `)` of the string (patterns end in `)`).
    Unquoted heredoc bodies are scanned like double-quoted text (`dq=True`): quotes and `#`
    are literal there. Each entry also carries its opener (`` ` `` or `$`/`<`/`>`).
    `end` bounds the scan (default: the whole string).
    Nesting deeper than _MAX_SUBST_DEPTH raises ParseError.
    """
    if level > _MAX_SUBST_DEPTH:
        raise ParseError(_TOO_DEEP)
    n = len(cmd) if end is None else end
    q = None
    depth = 0
    pending = []
    found = []
    while i < n:
        c = cmd[i]
        if q == "'":
            if c == "'":
                q = None
            i += 1
            continue
        if c == '\\':
            i += 2
            continue
        if close == '`' and c == '`':
            return i + 1, found
        if c == '`' or cmd.startswith('$(', i, n) or \
                (q is None and not dq and cmd.startswith(('<(', '>('), i, n)):
            body = i + (1 if c == '`' else 2)
            stop, inner = _walk(cmd, body, '`' if c == '`' else ')', level + 1, end=n)
            if cmd.startswith('$((', i, n):
                found.extend(inner)
            else:
                if c != '`' and _CASE.search(cmd, body, stop - 1):
                    stop = cmd.rfind(')', 0, n) + 1
                found.append((i, body, stop - 1, stop, c))
            i = stop
            continue
        if dq:
            i += 1
            continue
        if q == '"':
            if c == '"':
                q = None
        elif c == '\n' and pending:
            i, spans = _skip_bodies(cmd, i, pending, n)
            for b0, b1, quoted in spans:
                if not quoted:
                    found.extend(_walk(cmd, b0, None, level + 1, dq=True, end=b1)[1])
            continue
        elif c in '\'"':
            q = c
        elif c == '#' and (i == 0 or cmd[i - 1] in ' \t\n;&|('):
            while i < n and cmd[i] != '\n':
                i += 1
            continue
        elif cmd.startswith('<<', i) and not cmd.startswith('<<<', i):
            h = None if _in_arith(cmd[:n], i) else _heredoc_at(cmd[:n], i)
            if h:
                pending.append(h[:2])
                i = h[2]
                continue
            i += 2
            continue
        elif close == ')' and c == '(':
            depth += 1
        elif close == ')' and c == ')':
            if depth == 0:
                return i + 1, found
            depth -= 1
        i += 1
    if close:
        raise ParseError("unbalanced command substitution")
    return n, found


def _extract_substs(cmd):
    """Replace each $(...) / `...` with a placeholder word; return (text, bodies)."""
    if '$(' not in cmd and '`' not in cmd and '<(' not in cmd and '>(' not in cmd:
        return cmd, []
    found = _walk(cmd, 0, None)[1]
    if sum(f[3] - f[0] for f in found) > _MAX_BODY_BYTES:
        raise ParseError(_TOO_DEEP)
    out = []
    bodies = []
    last = 0
    for start, b0, b1, end, opener in found:
        out.append(cmd[last:start])
        out.append(SUBST_PH)
        body = cmd[b0:b1]
        if opener == '`':
            body = _BQ_ESC.sub(r'\1', body)
        bodies.append(body)
        last = end
    out.append(cmd[last:])
    return ''.join(out), bodies


def _extract_heredocs(cmd):
    lines = cmd.split('\n')
    kept = []
    bodies = []
    q = None
    i = 0
    while i < len(lines):
        line = lines[i]
        h, q = _find_heredoc(line, q)
        if not h:
            kept.append(line)
            i += 1
            continue
        j = i + 1
        body = []
        start, delim, _, end = h
        while j < len(lines) and lines[j].strip() != delim:
            body.append(lines[j])
            j += 1
        i = j + 1
        kept.append(line[:start] + ' ' + _HEREDOC_PH + ' ' + line[end:])
        prefix = _split_plain(line[:start])
        if (prefix and _is_shell_reader(prefix[-1].argv)) or _pipes_to_shell(line[end:]):
            text = '\n'.join(body)
            if len(body) > _MAX_BODY_LINES or len(text.encode('utf-8', 'replace')) > _MAX_BODY_BYTES:
                raise ParseError(_TOO_LARGE)
            bodies.append(text)
    return '\n'.join(kept), bodies


def split_commands(cmd):
    if len(cmd.encode('utf-8', 'replace')) > _MAX_INPUT:
        raise ParseError("command too long to inspect; run it as a script file")
    cmd, substs = _extract_substs(cmd)
    rest, bodies = _extract_heredocs(cmd)
    out = []
    prev = None
    for s in _split_plain(rest):
        out.append(s)
        if is_pipe(s.sep) and _is_shell_reader(s.argv):
            # The shell runs what the previous stage writes: only literal echo / printf
            # text is known; a heredoc body was inspected already.
            if prev is None:
                raise ParseError(_PIPE_ERR)
            if _HEREDOC_PH in prev.argv:
                if _drop_redirects(prev.argv) != ['cat', _HEREDOC_PH]:
                    raise ParseError(_PIPE_ERR)
            else:
                for inner in split_commands(_pipe_payload(prev.argv)):
                    inner.payload_of = inner.payload_of or 'pipe'
                    out.append(inner)
        prev = s
        p = _payload_of(s.argv)
        if p:
            for inner in split_commands(p[0]):
                inner.payload_of = inner.payload_of or p[1]
                out.append(inner)
    for body in bodies:
        for inner in split_commands(body):
            inner.payload_of = inner.payload_of or 'heredoc'
            out.append(inner)
    if substs:
        # One parse for all bodies: they share payload_of, and each body is quote-balanced
        # (the walker raised otherwise), so a newline keeps them apart.
        for inner in split_commands('\n'.join(substs)):
            inner.payload_of = inner.payload_of or 'subst'
            out.append(inner)
    return out
