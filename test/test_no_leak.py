"""Fail when a tracked file carries a ticket id, a personal path, an email, or a denylisted name."""
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Patterns are assembled from fragments so this file does not match itself.
_TICKET = re.compile('[A-Z]{2,6}[0-9]*' + '-' + '[0-9]{2,}')
_TICKET_OK = ('SHA-', 'ISO-', 'RFC-', 'UTF-')
_PATHS = (re.compile('/' + 'Users' + '/'), re.compile('/' + 'home' + '/'))
_EMAIL = re.compile(r'[\w.]+' + '@' + r'[\w.]+\.\w+')


def _tracked():
    out = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True, check=True).stdout
    return [p for p in out.split(b'\0') if p]


def _denylist():
    path = os.environ.get('HARDRAIL_DENYLIST')
    if not path or not os.path.isfile(path):
        return []
    with open(path, encoding='utf-8') as fh:
        return [ln.strip().lower() for ln in fh if ln.strip()]


def _scan(line, deny):
    hits = []
    for m in _TICKET.finditer(line):
        if not m.group(0).startswith(_TICKET_OK):
            hits.append(m.group(0))
    for rx in _PATHS + (_EMAIL,):
        hits.extend(m.group(0) for m in rx.finditer(line))
    low = line.lower()
    hits.extend(d for d in deny if d in low)
    return hits


class NoLeakTest(unittest.TestCase):
    def test_tracked_files_are_clean(self):
        deny = _denylist()
        found = []
        for raw in _tracked():
            rel = os.fsdecode(raw)
            if rel.split('/')[0] == '.git':
                continue
            try:
                with open(os.path.join(ROOT, rel), 'rb') as fh:
                    text = fh.read().decode('utf-8')
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                for hit in _scan(line, deny):
                    found.append('%s:%d: %s' % (rel, n, hit))
        self.assertEqual(found, [], 'leaks found:\n' + '\n'.join(found))


if __name__ == '__main__':
    unittest.main()
