import copy
import unittest

from core.config import DEFAULTS
from core.guard import decide
from test.core.guard_table import ROWS


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.config = copy.deepcopy(DEFAULTS)

    def test_table_size(self):
        self.assertGreaterEqual(len(ROWS), 40)

    def test_table(self):
        for command, action, rule in ROWS:
            with self.subTest(command=command):
                d = decide(command, "/tmp", self.config)
                self.assertEqual((d.action, d.rule), (action, rule), d.message)

    def test_warn_mode_downgrades_block(self):
        self.config["guard"]["mode"] = "warn"
        d = decide("pnpm test", "/tmp", self.config)
        self.assertEqual((d.action, d.rule), ("warn", "unscoped_test"))

    def test_message_has_fix(self):
        d = decide("pnpm test", "/tmp", self.config)
        self.assertIn("pnpm exec vitest --run", d.message)

    def test_message_on_single_line(self):
        for command, _, _ in ROWS:
            with self.subTest(command=command):
                self.assertNotIn("\n", decide(command, "/tmp", self.config).message)


if __name__ == "__main__":
    unittest.main()
