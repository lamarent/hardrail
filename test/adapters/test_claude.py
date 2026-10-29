import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOK = os.path.join(ROOT, "adapters", "claude", "pre-bash.sh")
FIXTURE = os.path.join(ROOT, "test", "fixtures", "pnpm-monorepo")


def run_hook(command=None, raw=None, path=None, hook_cwd=FIXTURE):
    if raw is None:
        raw = json.dumps({"tool_name": "Bash", "tool_input": {"command": command},
                          "cwd": hook_cwd, "session_id": "s"})
    env = dict(os.environ)
    if path is not None:
        env["PATH"] = path
    return subprocess.run(["bash", HOOK], input=raw, capture_output=True, text=True,
                          cwd=FIXTURE, env=env)


class ClaudeHookTest(unittest.TestCase):
    def _no_python_path(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for name in ("bash", "cat", "dirname", "printf"):
            src = shutil.which(name)
            os.symlink(src, os.path.join(d, name))
        return d

    def test_block_exit_2_stderr(self):
        r = run_hook("pnpm test")
        self.assertEqual(r.returncode, 2)
        self.assertTrue(r.stderr.startswith("BLOCKED [unscoped_test]"), r.stderr)
        self.assertEqual(r.stdout, "")

    def test_allow_exit_0(self):
        r = run_hook("git status")
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_fast_path_skips_python(self):
        r = run_hook("ls -la", path=self._no_python_path(), hook_cwd="/")  # fixture path contains "test"
        self.assertEqual((r.returncode, r.stderr, r.stdout), (0, "", ""))

    def test_missing_python_fails_open(self):
        r = run_hook("pnpm test", path=self._no_python_path())
        self.assertEqual(r.returncode, 0)
        self.assertIn("guard disabled", r.stderr)
        self.assertEqual(r.stdout, "")

    def test_warn_exit_0(self):
        r = run_hook("pnpm build")
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stderr.startswith("WARN [heavy]"), r.stderr)
        self.assertEqual(r.stdout, "")

    def test_bad_json_fails_open(self):
        r = run_hook(raw="{ test")  # keyword so the pre-filter lets it through
        self.assertEqual(r.returncode, 0)
        self.assertIn("guard error", r.stderr)
        self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
