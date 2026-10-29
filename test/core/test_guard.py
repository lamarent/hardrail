import copy
import os
import unittest

from core.config import DEFAULTS
from core.guard import _glob_match, decide
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

    def test_glob_match_star_and_question(self):
        self.assertTrue(_glob_match("pnpm bui?d*", "pnpm build --prod"))
        self.assertFalse(_glob_match("pnpm bui?d", "pnpm buiild"))
        self.assertTrue(_glob_match("a.b", "a.b"))
        self.assertFalse(_glob_match("a.b", "axb"))

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

    def test_package_script_message_relative_to_cwd(self):
        cwd = os.path.join(ROOT, "test/fixtures/pnpm-monorepo/packages/web")
        d = decide("pnpm test", cwd, self.config)
        self.assertEqual(d.action, "block")
        self.assertIn("exec vitest --run", d.message)
        self.assertNotIn("-C packages/web", d.message)
        self.assertNotIn("-C ", d.message)

    def _msg(self, command):
        return decide(command, "/tmp", self.config).message

    def test_watch_fix_docker_logs(self):
        self.assertIn("use docker compose logs --tail=200 api", self._msg("docker compose logs -f api"))

    def test_watch_fix_playwright_drops_flag(self):
        self.assertTrue(self._msg("pnpm exec playwright test --ui e2e/a.spec.ts")
                        .endswith("pnpm exec playwright test e2e/a.spec.ts"))

    def test_watch_fix_test_watch_script(self):
        self.assertIn("run the package's `test` script with a path", self._msg("pnpm test:watch"))

    def test_watch_fix_vitest(self):
        self.assertTrue(self._msg("vitest src/a.test.ts").endswith("pnpm exec vitest --run src/a.test.ts"))

    def test_watch_fix_jest(self):
        self.assertTrue(self._msg("jest --watch src/a.test.ts").endswith("jest --watchAll=false src/a.test.ts"))

    def test_heavy_message_neutral_and_actual_command(self):
        m = self._msg("pnpm -r build")
        self.assertEqual(m, "heavy command; run it outside the agent loop or in a background task: pnpm -r build")
        self.assertNotIn("runner", m)

    def test_chained_gated_names_gated_part(self):
        self.assertIn("run `git push origin main` as its own call",
                      self._msg("git status && git push origin main"))

    def test_message_on_single_line(self):
        for command, *_ in ROWS:
            with self.subTest(command=command):
                self.assertNotIn("\n", decide(command, "/tmp", self.config).message)


if __name__ == "__main__":
    unittest.main()
