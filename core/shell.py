"""Shell tokeniser: split a command line into simple commands and expand payloads."""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from typing import List, Optional

_SEPS = ';&|\n'
_SENT = {c: chr(0xE000 + i) for i, c in enumerate(_SEPS)}
_UNSENT = {v: k for k, v in _SENT.items()}
_SHELLS = ('bash', 'sh', 'zsh')
_HEREDOC = re.compile(r'(?<!<)<<(?!<)-?\s*(["\']?)([A-Za-z_]\w*)\1')
_C_FLAG = re.compile(r'-[A-Za-z]*c[A-Za-z]*')


@dataclass
class Simple:
    argv: List[str]
    raw: str
    payload_of: Optional[str] = None  # "sh -c" | "eval" | "heredoc"


class ParseError(Exception):
    pass


def _s(c: str) -> str:
    return _SENT.get(c, c)


def _protect(cmd: str) -> str:
    """Replace separator chars that are quoted, escaped or part of a redirect with sentinels."""
    out: List[str] = []
    q = None
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if q == "'":
            if c == "'":
                q = None
        elif q == '"':
            if c == '\\' and i + 1 < n:
                out.append(c)
                out.append(_s(cmd[i + 1]))
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
                out.append(_s(nxt) if nxt in _SEPS else c + nxt)
                i += 2
                continue
            if c in '\'"':
                q = c
            elif c == '&' and ((i > 0 and cmd[i - 1] in '<>') or (i + 1 < n and cmd[i + 1] == '>')):
                out.append(_SENT['&'])
                i += 1
                continue
            else:
                out.append(c)
                i += 1
                continue
        out.append(_s(c))
        i += 1
    return ''.join(out)


def _restore(word: str) -> str:
    return ''.join(_UNSENT.get(c, c) for c in word)


def _split_plain(text: str) -> List[Simple]:
    lex = shlex.shlex(_protect(text), posix=True, punctuation_chars=_SEPS)
    lex.whitespace = ' \t\r'
    lex.whitespace_split = True
    lex.commenters = ''
    try:
        tokens = list(lex)
    except ValueError as e:
        raise ParseError(str(e)) from e
    result: List[Simple] = []
    cur: List[str] = []

    def flush() -> None:
        if cur:
            result.append(Simple(argv=list(cur), raw=' '.join(cur)))
            cur.clear()

    for tok in tokens:
        if tok and all(c in _SEPS for c in tok):
            flush()
        else:
            cur.append(_restore(tok))
    flush()
    return result


def _payload_of(argv: List[str]):
    """Return (text, kind) for the first sh -c / eval payload in argv, else None."""
    if argv and argv[0] == 'eval' and len(argv) > 1:
        return ' '.join(argv[1:]), 'eval'
    for j, w in enumerate(argv):
        if os.path.basename(w) not in _SHELLS:
            continue
        k = j + 1
        while k < len(argv) and argv[k].startswith('-'):
            if _C_FLAG.fullmatch(argv[k]) and k + 1 < len(argv):
                return argv[k + 1], 'sh -c'
            k += 1
    return None


def _is_shell_reader(argv: Optional[List[str]]) -> bool:
    return bool(argv) and os.path.basename(argv[0]) in _SHELLS and all(a.startswith('-') for a in argv[1:])


def _extract_heredocs(cmd: str):
    lines = cmd.split('\n')
    kept: List[str] = []
    bodies: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _HEREDOC.search(line)
        if not m:
            kept.append(line)
            i += 1
            continue
        j = i + 1
        body: List[str] = []
        while j < len(lines) and lines[j].strip() != m.group(2):
            body.append(lines[j])
            j += 1
        i = j + 1
        kept.append(line[:m.start()] + line[m.end():])
        prefix = _split_plain(line[:m.start()])
        if prefix and _is_shell_reader(prefix[-1].argv):
            bodies.append('\n'.join(body))
    return '\n'.join(kept), bodies


def split_commands(cmd: str) -> List[Simple]:
    rest, bodies = _extract_heredocs(cmd)
    out: List[Simple] = []
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
    return out
