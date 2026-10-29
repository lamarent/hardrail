"""Seeded random commands: decide() must return a Decision and never raise."""
import os
import random
import time
import unittest

from core.config import DEFAULTS
from core.guard import Decision, decide

FRAGMENTS = [
    "git push -f", "pnpm test", "$(", "`", "<<EOF", "'", '"', "|", "&&", "\n",
    "$'x'", "env -i", "{", ")", "#", "héllo", "テスト", "\x00",
    "sudo -i", "xargs", "<<<", "<(", "bash -c", "| bash", "source", "eval", "-C", "--filter",
    "pnpm exec", "find . -exec",
]
CATCH_ALL = "guard could not evaluate"
CASES = 300
CASE_BUDGET = 0.5


class RobustnessTest(unittest.TestCase):
    def test_random_commands_never_raise(self):
        seed = int(os.environ.get("HARDRAIL_FUZZ_SEED", "1"))
        rng = random.Random(seed)
        for i in range(CASES):
            cmd = " ".join(rng.choice(FRAGMENTS) for _ in range(rng.randint(1, 12)))
            where = f"seed={seed} case={i} cmd={cmd!r}"
            start = time.perf_counter()
            try:
                d = decide(cmd, "/tmp", DEFAULTS)
            except Exception as e:  # noqa: BLE001 - the point is that nothing escapes
                self.fail(f"{where} raised {type(e).__name__}: {e}")
            elapsed = time.perf_counter() - start
            self.assertIsInstance(d, Decision, where)
            self.assertIn(d.action, {"allow", "warn", "block"}, where)
            self.assertFalse(d.message.startswith(CATCH_ALL), f"{where} -> {d.message}")
            self.assertLess(elapsed, CASE_BUDGET, where)


if __name__ == "__main__":
    unittest.main()
