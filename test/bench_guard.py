"""Latency benchmark for the Claude pre-bash hook. Run from the repo root."""
import json
import os
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, 'adapters', 'claude', 'pre-bash.sh')
CWD = os.path.join(ROOT, 'test', 'fixtures', 'pnpm-monorepo')
RUNS = 50
WARMUP = 3
LIMIT_MS = 100.0

HEREDOC = "bash <<'EOF'\n" + ''.join('echo line %05d padding padding\n' % i for i in range(640)) + '\nEOF'

CASES = [
    ('fast path (git status)', 'git status'),
    ('python path (vitest)', 'pnpm exec vitest --run src/a.test.ts'),
    ('20 KB heredoc', HEREDOC),
]


def once(payload):
    start = time.perf_counter()
    subprocess.run(['bash', HOOK], input=payload, capture_output=True, text=True, cwd=ROOT)
    return (time.perf_counter() - start) * 1000.0


def main():
    failed = []
    for name, command in CASES:
        payload = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': command}, 'cwd': CWD})
        for _ in range(WARMUP):
            once(payload)
        times = [once(payload) for _ in range(RUNS)]
        q = statistics.quantiles(times, n=100)
        p50, p95 = q[49], q[94]
        print('%s: p50=%.1f p95=%.1f' % (name, p50, p95))
        if p95 >= LIMIT_MS:
            failed.append(name)
    if failed:
        print('p95 >= %d ms: %s' % (LIMIT_MS, ', '.join(failed)), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
