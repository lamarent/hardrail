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

HEREDOC = "cat <<'EOF'\n" + ''.join('echo test %05d padding padding\n' % i for i in range(640)) + '\nEOF'
CAP = "bash <<'EOF'\n" + 'echo test\n' * 201 + 'EOF'

CASES = [
    ('fast path (git status)', 'git status', 0),
    ('python path (vitest)', 'pnpm exec vitest --run src/a.test.ts', 0),
    ('20 KB heredoc (cat)', HEREDOC, 0),
    ('cap path (bash heredoc 201 lines)', CAP, 2),
]


def once(payload):
    start = time.perf_counter()
    r = subprocess.run(['bash', HOOK], input=payload, capture_output=True, text=True, cwd=ROOT)
    return (time.perf_counter() - start) * 1000.0, r.returncode


def main():
    failed = []
    for name, command, want in CASES:
        payload = json.dumps({'session_id': 's', 'cwd': CWD, 'hook_event_name': 'PreToolUse',
                              'tool_name': 'Bash', 'tool_input': {'command': command}})
        runs = [once(payload) for _ in range(WARMUP + RUNS)]
        if any(rc != want for _, rc in runs):
            print('%s: unexpected exit code (want %d, got %s)' % (name, want, sorted({rc for _, rc in runs})),
                  file=sys.stderr)
            failed.append(name)
            continue
        times = [t for t, _ in runs[WARMUP:]]
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
