"""Shell tokeniser: split a command line into simple commands and expand payloads."""

import os
import re
import shlex

_SEPS = ';&|\n'
_SENT = {c: chr(0xE000 + i) for i, c in enumerate(_SEPS)}
_UNSENT = {v: k for k, v in _SENT.items()}
_SHELLS = ('bash', 'sh', 'zsh', 'dash')
_HEREDOC = re.compile(r'<<-?\s*(["\']?)([A-Za-z_]\w*)\1')
_C_FLAG = re.compile(r'-[A-Za-z]*c[A-Za-z]*')


class Simple:
    __slots__ = ("argv", "raw", "payload_of")  # payload_of: None | "sh -c" | "eval" | "heredoc" | "subst"

    def __init__(self, argv, raw, payload_of=None):
        self.argv = argv
        self.raw = raw
        self.payload_of = payload_of

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
_SUBST_PH = '_subst_'


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


def _split_plain(text):
    prot, idx = _protect(text)
    lex = shlex.shlex(prot, posix=True, punctuation_chars=_SEPS)
    lex.whitespace = ' \t\r'
    lex.whitespace_split = True
    lex.commenters = ''
    try:
        tokens = list(lex)
    except ValueError as e:
        raise ParseError(str(e)) from e
    spans = _spans(prot)
    use_spans = len(spans) == len(tokens)
    result = []
    cur = []
    first = last = 0

    def flush():
        if cur:
            if use_spans:
                raw = text[idx[first]:idx[last - 1] + 1]
            else:
                raw = ' '.join(cur)
            result.append(Simple(argv=list(cur), raw=raw))
            cur.clear()

    for k, tok in enumerate(tokens):
        if tok and all(c in _SEPS for c in tok):
            flush()
        else:
            if not cur and use_spans:
                first = spans[k][0]
            if use_spans:
                last = spans[k][1]
            cur.append(_restore(tok))
    flush()
    return result


def _shell_scan(argv, j):
    """Scan the flags after the shell word at argv[j].

    Returns ('c', payload_index) for a -c flag, ('flags', len) if only flags
    follow or -s (read from stdin) is given, else None. The argument after -o / -O is skipped.
    """
    k = j + 1
    while k < len(argv) and argv[k].startswith('-'):
        if argv[k] in ('-o', '-O'):
            k += 2
            continue
        if _C_FLAG.fullmatch(argv[k]) and k + 1 < len(argv):
            return 'c', k + 1
        if argv[k] == '-s':
            return 'flags', k
        k += 1
    return ('flags', k) if k >= len(argv) else None


def _payload_of(argv):
    """Return (text, kind) for the first sh -c / eval payload in argv, else None."""
    if argv and argv[0] == 'eval' and len(argv) > 1:
        return ' '.join(argv[1:]), 'eval'
    for j, w in enumerate(argv):
        if w.startswith('<<<'):
            text = w[3:] if len(w) > 3 else (argv[j + 1] if j + 1 < len(argv) else '')
            if _is_shell_reader(argv[:j] + argv[j + (1 if len(w) > 3 else 2):]):
                _check_size(text)
                return text, 'heredoc'
    for j, w in enumerate(argv):
        if os.path.basename(w) in _SHELLS:
            r = _shell_scan(argv, j)
            if r and r[0] == 'c':
                return argv[r[1]], 'sh -c'
    return None


def _is_shell_reader(argv):
    if not argv:
        return False
    for j, w in enumerate(argv):
        if os.path.basename(w) in _SHELLS:
            r = _shell_scan(argv, j)
            if r and r[0] == 'flags':
                return True
    return False


def _find_heredoc(line, q):
    """First `<<` outside quotes, comments and escapes. Returns (match, quote_state)."""
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
                m = _HEREDOC.match(line, i)
                if m:
                    return m, q
        i += 1
    return None, q


def _pipes_to_shell(after):
    """True when a later stage of the pipeline in `after` is a shell reading stdin."""
    prot = _protect(after)[0]
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


def _skip_bodies(cmd, i, pending):
    """cmd[i] is a newline after heredoc operators; skip their bodies.

    Returns the index of the newline that ends the last delimiter line (or len(cmd)).
    """
    n = len(cmd)
    for delim in pending:
        while i < n:
            j = cmd.find('\n', i + 1)
            j = n if j < 0 else j
            line = cmd[i + 1:j]
            i = j
            if line.strip() == delim:
                break
    del pending[:]
    return i


def _walk(cmd, i, close):
    """Scan cmd from i for command substitutions.

    `close` is None at the top level, else ')' or '`' for the substitution being scanned.
    Returns (index after the closing char, [(start, body_start, body_end, end)]) for the
    substitutions found directly at this level (nested ones are found when the body is split).
    Process substitutions `<(...)` / `>(...)` count too. Single quotes and heredoc bodies
    are skipped; `$((` arithmetic yields no body.
    """
    n = len(cmd)
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
        if c == '`' or cmd.startswith('$(', i) or (q is None and cmd.startswith(('<(', '>('), i)):
            body = i + (1 if c == '`' else 2)
            end = _walk(cmd, body, '`' if c == '`' else ')')[0]
            if not cmd.startswith('$((', i):
                found.append((i, body, end - 1, end))
            i = end
            continue
        if q == '"':
            if c == '"':
                q = None
        elif c == '\n' and pending:
            i = _skip_bodies(cmd, i, pending)
            continue
        elif c in '\'"':
            q = c
        elif c == '#' and (i == 0 or cmd[i - 1] in ' \t\n;&|('):
            while i < n and cmd[i] != '\n':
                i += 1
            continue
        elif cmd.startswith('<<', i) and not cmd.startswith('<<<', i):
            m = _HEREDOC.match(cmd, i)
            if m:
                pending.append(m.group(2))
                i = m.end()
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
    out = []
    bodies = []
    last = 0
    for start, b0, b1, end in found:
        out.append(cmd[last:start])
        out.append(_SUBST_PH)
        bodies.append(cmd[b0:b1])
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
        m, q = _find_heredoc(line, q)
        if not m:
            kept.append(line)
            i += 1
            continue
        j = i + 1
        body = []
        while j < len(lines) and lines[j].strip() != m.group(2):
            body.append(lines[j])
            j += 1
        i = j + 1
        kept.append(line[:m.start()] + line[m.end():])
        prefix = _split_plain(line[:m.start()])
        if (prefix and _is_shell_reader(prefix[-1].argv)) or _pipes_to_shell(line[m.end():]):
            text = '\n'.join(body)
            if len(body) > _MAX_BODY_LINES or len(text.encode('utf-8', 'replace')) > _MAX_BODY_BYTES:
                raise ParseError(_TOO_LARGE)
            bodies.append(text)
    return '\n'.join(kept), bodies


def split_commands(cmd):
    cmd, substs = _extract_substs(cmd)
    rest, bodies = _extract_heredocs(cmd)
    out = []
    for s in _split_plain(rest):
        out.append(s)
        p = _payload_of(s.argv)
        if p:
            for inner in split_commands(p[0]):
                inner.payload_of = inner.payload_of or p[1]
                out.append(inner)
    for body in bodies:
        for inner in split_commands(body):
            inner.payload_of = inner.payload_of or 'heredoc'
            out.append(inner)
    for body in substs:
        for inner in split_commands(body):
            inner.payload_of = inner.payload_of or 'subst'
            out.append(inner)
    return out
