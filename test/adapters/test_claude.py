import json
import os
import sys
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from core.config import DEFAULTS  # noqa: E402
HOOK = os.path.join(ROOT, "adapters", "claude", "pre-bash.sh")
FIXTURE = os.path.join(ROOT, "test", "fixtures", "pnpm-monorepo")


def run_hook(command=None, raw=None, path=None, hook_cwd=FIXTURE, hook=HOOK, extra_env=None):
    if raw is None:
        raw = json.dumps({"session_id": "s", "cwd": hook_cwd, "hook_event_name": "PreToolUse",
                          "tool_name": "Bash", "tool_input": {"command": command}})
    env = dict(os.environ)
    if path is not None:
        env["PATH"] = path
    env.update(extra_env or {})
    return subprocess.run(["bash", hook], input=raw, capture_output=True, text=True,
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
        r = run_hook("ls -la", path=self._no_python_path())
        self.assertEqual((r.returncode, r.stderr, r.stdout), (0, "", ""))

    def test_missing_python_fails_open(self):
        r = run_hook("pnpm test", path=self._no_python_path())
        self.assertEqual(r.returncode, 0)
        self.assertIn("guard disabled", r.stderr)
        self.assertEqual(r.stdout, "")

    def test_embedded_brace_quote_does_not_hide_command(self):
        r = run_hook("echo '\"}, ' ; pnpm test")
        self.assertEqual(r.returncode, 2)
        self.assertTrue(r.stderr.startswith("BLOCKED [unscoped_test]"), r.stderr)

    def test_embedded_brace_quote_before_force_push(self):
        r = run_hook("echo '\"},' && git push -f")
        self.assertEqual(r.returncode, 2)
        self.assertTrue(r.stderr.startswith("BLOCKED [force_push]"), r.stderr)

    def test_warn_exit_0(self):
        r = run_hook("pnpm build")
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stderr.startswith("WARN [heavy]"), r.stderr)
        self.assertEqual(r.stdout, "")

    def test_prefilter_covers_every_gated_and_heavy_entry(self):
        # Without python3 on PATH, reaching the python step prints "guard disabled";
        # a command dropped by the keyword pre-filter prints nothing.
        path = self._no_python_path()
        for entry in DEFAULTS["guard"]["gatedActions"] + DEFAULTS["heavy"]:
            with self.subTest(entry=entry):
                r = run_hook("%s --zz" % entry, path=path)
                self.assertIn("guard disabled", r.stderr)

    def test_chained_stash_blocks(self):
        r = run_hook("git stash && git status")
        self.assertEqual(r.returncode, 2)
        self.assertTrue(r.stderr.startswith("BLOCKED [chained_gated]"), r.stderr)

    def test_invalid_config_falls_back_to_defaults(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "hardrail.config.json"), "w", encoding="utf-8") as f:
            f.write('{"guard": {"mode": "warn"},}')
        r = run_hook("pnpm test", hook_cwd=d)
        self.assertEqual(r.returncode, 2)
        lines = r.stderr.splitlines()
        self.assertTrue(lines[0].startswith("hardrail: config ignored ("), r.stderr)
        self.assertTrue(lines[0].endswith("), using defaults"), r.stderr)
        self.assertTrue(lines[1].startswith("BLOCKED [unscoped_test]"), r.stderr)

    def test_wrong_typed_config_still_enforces(self):
        for text in ('{"guard": []}', '{"guard": {"gatedActions": null}}'):
            with self.subTest(config=text):
                d = tempfile.mkdtemp()
                self.addCleanup(shutil.rmtree, d, True)
                with open(os.path.join(d, "hardrail.config.json"), "w", encoding="utf-8") as f:
                    f.write(text)
                r = run_hook("git commit -m x && ls", hook_cwd=d)
                self.assertEqual(r.returncode, 2, r.stderr)
                lines = r.stderr.splitlines()
                self.assertTrue(lines[0].startswith("hardrail: config ignored (guard"), r.stderr)
                self.assertTrue(lines[1].startswith("BLOCKED [chained_gated]"), r.stderr)

    def _config_dir(self, text):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "hardrail.config.json"), "w", encoding="utf-8") as f:
            f.write(text)
        return d

    def test_unknown_key_warns_and_applies_config(self):
        r = run_hook("pnpm test", hook_cwd=self._config_dir('{"bogus": 1, "guard": {"mode": "warn"}}'))
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stderr.splitlines()
        self.assertEqual(lines[0], "hardrail: unknown config key bogus ignored")
        self.assertTrue(lines[1].startswith("WARN [unscoped_test]"), r.stderr)

    def test_schema_key_is_silent(self):
        r = run_hook("pnpm test", hook_cwd=self._config_dir('{"$schema": "x.json"}'))
        self.assertEqual(r.returncode, 2)
        self.assertTrue(r.stderr.startswith("BLOCKED [unscoped_test]"), r.stderr)

    def test_tail_and_podman_follow_reach_the_guard(self):
        for command in ("tail -f x", "podman logs -f x"):
            with self.subTest(command=command):
                r = run_hook(command)
                self.assertEqual(r.returncode, 2)
                self.assertTrue(r.stderr.startswith("BLOCKED [watch_mode]"), r.stderr)

    def test_snippet_uses_project_dir(self):
        with open(os.path.join(ROOT, "adapters", "claude", "hooks.snippet.json"), encoding="utf-8") as f:
            snippet = json.load(f)
        hook = snippet["hooks"]["PreToolUse"][0]
        self.assertEqual(hook["matcher"], "Bash")
        self.assertEqual(hook["hooks"][0]["command"], '"$CLAUDE_PROJECT_DIR"/.claude/hardrail/pre-bash.sh')

    def _symlinked_hook(self, with_python):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        real, sym = os.path.join(d, "real"), os.path.join(d, "sym")
        os.mkdir(real)
        os.mkdir(sym)
        adapter = os.path.join(ROOT, "adapters", "claude")
        shutil.copy(os.path.join(adapter, "pre-bash.sh"), real)
        if with_python:
            shutil.copy(os.path.join(adapter, "pre_bash.py"), real)
            shutil.copytree(os.path.join(ROOT, "core"), os.path.join(real, "core"),
                            ignore=shutil.ignore_patterns("__pycache__"))
        link = os.path.join(sym, "pre-bash.sh")
        os.symlink(os.path.join(real, "pre-bash.sh"), link)
        return link

    def test_symlinked_hook_fails_open_when_shim_missing(self):
        r = run_hook("pnpm test", hook=self._symlinked_hook(False))
        self.assertEqual(r.returncode, 0)
        self.assertIn("guard disabled", r.stderr)

    def test_symlinked_hook_still_blocks_when_shim_present(self):
        r = run_hook("pnpm test", hook=self._symlinked_hook(True))
        self.assertEqual(r.returncode, 2, r.stderr)

    def test_event_without_command_is_silent(self):
        r = run_hook(raw='{"tool_name":"Edit","tool_input":{"file_path":"x"},"cwd":"/tmp"}')
        self.assertEqual((r.returncode, r.stderr), (0, ""))

    def test_non_string_command_is_silent(self):
        r = run_hook(raw='{"tool_name":"Bash","tool_input":{"command":["test"]},"cwd":"/tmp"}')
        self.assertEqual((r.returncode, r.stderr), (0, ""))

    def test_empty_stdin_is_silent(self):
        r = run_hook(raw="")
        self.assertEqual((r.returncode, r.stderr), (0, ""))

    def test_config_present_skips_fast_path(self):
        # "kubectl apply" hits no keyword, so only the config skip can send it to python.
        d = self._config_dir('{"guard": {"gatedActions": ["kubectl apply"]}}')
        env = {"CLAUDE_PROJECT_DIR": d}
        raw = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls && kubectl apply"}, "cwd": d})
        r = run_hook(raw=raw, extra_env=env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("chained_gated", r.stderr)
        os.remove(os.path.join(d, "hardrail.config.json"))
        raw = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls && kubectl apply"}, "cwd": "/"})
        r = run_hook(raw=raw, extra_env=env)
        self.assertEqual((r.returncode, r.stderr), (0, ""))

    def test_snippet_has_timeout(self):
        with open(os.path.join(ROOT, "adapters", "claude", "hooks.snippet.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"], 10)

    def test_bad_json_fails_open(self):
        r = run_hook(raw="{")  # no command key: pre-filter falls through to python
        self.assertEqual(r.returncode, 0)
        self.assertIn("guard error", r.stderr)
        self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
