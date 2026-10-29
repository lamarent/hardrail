import copy
import os
import unittest

from core.config import DEFAULTS
from core.guard import decide
from test.core.guard_table import ROWS

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.config = copy.deepcopy(DEFAULTS)

    def test_table_size(self):
        self.assertGreaterEqual(len(ROWS), 40)

    def test_table(self):
        for command, action, rule, *cwd in ROWS:
            with self.subTest(command=command):
                d = decide(command, os.path.join(ROOT, *cwd) if cwd else "/tmp", self.config)
                self.assertEqual((d.action, d.rule), (action, rule), d.message)

    def test_warn_mode_downgrades_block(self):
        self.config["guard"]["mode"] = "warn"
        d = decide("pnpm test", "/tmp", self.config)
        self.assertEqual((d.action, d.rule), ("warn", "unscoped_test"))

    def test_message_has_fix(self):
        d = decide("pnpm test", "/tmp", self.config)
        self.assertIn("pnpm exec vitest --run", d.message)

    def test_package_script_with_path_message(self):
        cwd = os.path.join(ROOT, "test/fixtures/pnpm-monorepo")
        d = decide("pnpm -C packages/web test src/a.test.ts", cwd, self.config)
        self.assertIn("pnpm -C packages/web exec vitest --run src/a.test.ts", d.message)

    def test_all_message_mentions_filter(self):
        d = decide("pnpm -r test", "/tmp", self.config)
        self.assertIn("--filter <name>", d.message)

    def test_message_on_single_line(self):
        for command, *_ in ROWS:
            with self.subTest(command=command):
                self.assertNotIn("\n", decide(command, "/tmp", self.config).message)


if __name__ == "__main__":
    unittest.main()
