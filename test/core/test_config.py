import json
import os
import tempfile
import unittest

from core import config

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures", "config")


def fixture_root(name):
    """Make a temp repo root whose config is the named fixture."""
    root = tempfile.mkdtemp()
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as src:
        data = src.read()
    with open(os.path.join(root, "hardrail.config.json"), "w", encoding="utf-8") as dst:
        dst.write(data)
    return root


class ConfigTest(unittest.TestCase):
    def test_defaults_gate_install_aliases(self):
        gated = config.DEFAULTS["guard"]["gatedActions"]
        self.assertIn("npm ci", gated)
        self.assertIn("pnpm i", gated)

    def test_minimal_gets_defaults(self):
        cfg = config.load(fixture_root("minimal.json"))
        self.assertEqual(cfg["guard"]["mode"], "block")
        self.assertIn("git push", cfg["guard"]["gatedActions"])

    def test_full_overrides(self):
        cfg = config.load(fixture_root("full.json"))
        self.assertIn("backend", cfg["packages"])
        self.assertEqual(cfg["guard"]["mode"], "block")
        self.assertNotIn("git reset", cfg["guard"]["gatedActions"])
        self.assertTrue(cfg["worktree"]["required"])
        self.assertEqual(cfg["defaults"]["lint"], "biome check")

    def test_deep_merge_keeps_default_keys(self):
        root = tempfile.mkdtemp()
        with open(os.path.join(root, "hardrail.config.json"), "w", encoding="utf-8") as f:
            json.dump({"guard": {"mode": "warn"}}, f)
        cfg = config.load(root)
        self.assertEqual(cfg["guard"]["mode"], "warn")
        self.assertIn("git push", cfg["guard"]["gatedActions"])

    def test_missing_file_returns_copy(self):
        cfg = config.load(tempfile.mkdtemp())
        self.assertEqual(cfg, config.DEFAULTS)
        cfg["guard"]["gatedActions"].append("x")
        self.assertNotIn("x", config.DEFAULTS["guard"]["gatedActions"])

    def _write(self, text):
        root = tempfile.mkdtemp()
        with open(os.path.join(root, "hardrail.config.json"), "w", encoding="utf-8") as f:
            f.write(text)
        return root

    def test_invalid_json_falls_back_to_defaults(self):
        cfg = config.load(self._write('{"guard": {"mode": "warn"},}'))
        self.assertIn("hardrail.config.json", cfg["_load_error"])
        self.assertEqual(cfg["guard"], config.DEFAULTS["guard"])
        self.assertNotIn("_load_error", config.DEFAULTS)

    def test_non_object_falls_back_to_defaults(self):
        cfg = config.load(self._write('["guard"]'))
        self.assertEqual(cfg["_load_error"], "hardrail.config.json is not a JSON object")
        self.assertEqual(cfg["guard"]["mode"], "block")

    def test_wrong_typed_section_reverts(self):
        cfg = config.load(self._write('{"guard": [], "heavy": "build"}'))
        self.assertEqual(cfg["guard"], config.DEFAULTS["guard"])
        self.assertEqual(cfg["heavy"], config.DEFAULTS["heavy"])
        self.assertEqual(cfg["_load_error"], "guard must be dict; heavy must be list")

    def test_wrong_typed_nested_key_reverts(self):
        cfg = config.load(self._write('{"guard": {"mode": "warn", "gatedActions": null}}'))
        self.assertEqual(cfg["guard"]["mode"], "warn")
        self.assertEqual(cfg["guard"]["gatedActions"], config.DEFAULTS["guard"]["gatedActions"])
        self.assertEqual(cfg["_load_error"], "guard.gatedActions must be list")

    def test_non_string_list_item_reverts(self):
        cfg = config.load(self._write('{"guard": {"gatedActions": ["git push", 1]}}'))
        self.assertEqual(cfg["guard"]["gatedActions"], config.DEFAULTS["guard"]["gatedActions"])

    def test_find_root_walks_up(self):
        root = fixture_root("minimal.json")
        deep = os.path.join(root, "a", "b")
        os.makedirs(deep)
        self.assertEqual(os.path.realpath(config.find_root(deep)), os.path.realpath(root))

    def test_find_root_none(self):
        self.assertIsNone(config.find_root(tempfile.mkdtemp()))

    def test_unknown_key_warns(self):
        cfg = config.load(self._write('{"bogus": 1}'))
        self.assertIn("unknown key bogus", cfg["_load_error"])
        self.assertEqual(cfg["bogus"], 1)

    def test_schema_keys_are_known(self):
        cfg = config.load(fixture_root("full.json"))
        self.assertNotIn("_load_error", cfg)

    def test_schema_packages_shape(self):
        path = os.path.join(os.path.dirname(__file__), "..", "..", "schema", "hardrail.config.schema.json")
        with open(path, encoding="utf-8") as f:
            schema = json.load(f)
        item = schema["properties"]["packages"]["additionalProperties"]
        self.assertEqual(set(item["properties"]), {"test"})
        self.assertEqual(item["properties"]["test"], {"type": "string"})


if __name__ == "__main__":
    unittest.main()
