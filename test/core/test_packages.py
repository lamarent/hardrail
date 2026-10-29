import os
import unittest

from core.packages import Package, resolve_package
from core.shell import split_commands

HERE = os.path.dirname(os.path.abspath(__file__))
FX = os.path.abspath(os.path.join(HERE, "..", "fixtures", "pnpm-monorepo"))


def resolve(command, cwd=FX):
    simples = split_commands(command)
    last = simples[-1]
    prev = simples[-2] if len(simples) > 1 else None
    return resolve_package(last, cwd, {}, FX, prev=prev)


class PackagesTest(unittest.TestCase):
    def test_dash_C(self):
        p = resolve("pnpm -C packages/api test")
        self.assertIsInstance(p, Package)
        self.assertEqual(p.name, "@fx/api")
        self.assertEqual(p.root, os.path.join(FX, "packages", "api"))

    def test_filter_name(self):
        self.assertEqual(resolve("pnpm --filter @fx/web test").name, "@fx/web")

    def test_filter_glob_is_all(self):
        for flt in ("'*'", "'@fx/*'", "'...@fx/api'", "'[ab]'"):
            self.assertEqual(resolve(f"pnpm --filter {flt} test"), "all", flt)

    def test_recursive_is_all(self):
        for cmd in ("pnpm -r test", "pnpm --recursive test", "pnpm --parallel test"):
            self.assertEqual(resolve(cmd), "all", cmd)

    def test_cwd_ancestor(self):
        cwd = os.path.join(FX, "packages", "web")
        self.assertEqual(resolve("pnpm test", cwd).name, "@fx/web")

    def test_cd_prefix(self):
        self.assertEqual(resolve("cd packages/api && pnpm test").name, "@fx/api")

    def test_cd_prefix_word_boundary(self):
        p = resolve("abcd packages/api; pnpm test")
        self.assertEqual(p.name, "@fx/root")

    def test_script_with_path_detected(self):
        self.assertEqual(resolve("pnpm --filter @fx/web test").test_script, "vitest src/")

    def test_nothing_found(self):
        self.assertIsNone(resolve("pnpm test", "/"))


if __name__ == "__main__":
    unittest.main()
