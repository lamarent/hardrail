import os
import unittest

from core.packages import Package, _workspace_globs, _yaml_globs, find_workspace_root, resolve_package
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

    def test_equals_forms(self):
        self.assertEqual(resolve("pnpm --dir=packages/web test").name, "@fx/web")
        self.assertEqual(resolve("pnpm -C=packages/web test").name, "@fx/web")
        self.assertEqual(resolve("pnpm --filter=@fx/api test").name, "@fx/api")

    def test_path_filter(self):
        self.assertEqual(resolve("pnpm --filter ./packages/web test").name, "@fx/web")
        self.assertEqual(resolve("pnpm --filter packages/api test").name, "@fx/api")

    def test_fail_closed_is_all(self):
        for cmd in ("pnpm --filter nope test", "pnpm -C packages/none test",
                    "pnpm -C packages test", "pnpm -C ../../.. test", "pnpm -C ~ test",
                    "pnpm --filter '!x' test", "pnpm --filter=!@fx/api test",
                    "pnpm --filter '{packages/*}' test",
                    "pnpm --filter @fx/api --filter @fx/web test",
                    "pnpm --filter ../../.. test", "cd ../../.. && pnpm test",
                    "pnpm --recursive=true test", "pnpm -rF @fx/api test",
                    "pnpm -wr test", "pnpm -r -F @fx/api test"):
            self.assertEqual(resolve(cmd), "all", cmd)

    def test_cluster_value_and_root_flag(self):
        self.assertEqual(resolve("pnpm -F @fx/api test").name, "@fx/api")
        self.assertEqual(resolve("pnpm -F@fx/api test").name, "@fx/api")
        self.assertEqual(resolve("pnpm -w test").name, "@fx/root")
        self.assertEqual(resolve("pnpm --workspace-root test").name, "@fx/root")

    def test_yaml_forms(self):
        forms = os.path.join(HERE, "..", "fixtures", "yaml-forms")
        for form in ("inline", "flow", "comment", "quoted"):
            with self.subTest(form=form):
                self.assertEqual(_workspace_globs(os.path.join(forms, form)), ['packages/*'])

    def test_yaml_nested_packages_key_is_ignored(self):
        self.assertEqual(_yaml_globs("catalogs:\n  packages:\n    - evil/*\npackages:\n  - 'packages/*'\n"),
                         ['packages/*'])
        self.assertEqual(_yaml_globs("catalogs:\n  foo:\n    packages: [evil/*]"), [])

    def test_find_workspace_root_from_package_dir(self):
        self.assertEqual(find_workspace_root(os.path.join(FX, "packages", "api", "src")), FX)
        self.assertIsNone(find_workspace_root("/"))

    def test_filter_dot_is_cwd_package(self):
        cwd = os.path.join(FX, "packages", "api", "src")
        self.assertEqual(resolve("pnpm --filter . test", cwd).name, "@fx/api")
        self.assertEqual(resolve("pnpm --filter ../web test", os.path.join(FX, "packages", "api")).name,
                         "@fx/web")
        self.assertEqual(resolve("pnpm --filter '{packages/web}' test").name, "@fx/web")

    def test_nothing_found(self):
        self.assertIsNone(resolve("pnpm test", "/"))


if __name__ == "__main__":
    unittest.main()
