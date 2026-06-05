import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import manifest  # noqa: E402


def _write(tmp, obj):
    path = os.path.join(tmp, "m.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    return path


class TestLoadManifest(unittest.TestCase):
    def test_loads_project_window_and_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {
                "project": "avm-tf-storage",
                "window": {"from": "2026-03-01", "to": "2026-03-31"},
                "repos": [
                    {"owner": "Azure", "repo": "terraform-azurerm-avm-res-storage-storageaccount",
                     "registry": "Azure/avm-res-storage-storageaccount/azurerm"},
                    {"owner": "Azure", "repo": "terraform-azurerm-avm-res-keyvault-vault"},
                ],
            })
            m = manifest.load_manifest(path)
        self.assertEqual(m["project"], "avm-tf-storage")
        self.assertEqual(m["from"], "2026-03-01")
        self.assertEqual(m["to"], "2026-03-31")
        self.assertEqual(len(m["repos"]), 2)
        self.assertEqual(m["repos"][0]["registry"],
                         "Azure/avm-res-storage-storageaccount/azurerm")
        self.assertIsNone(m["repos"][1]["registry"])  # optional, defaults None

    def test_member_slugs(self):
        m = {"repos": [{"owner": "Azure", "repo": "a"},
                       {"owner": "Azure", "repo": "b"}]}
        self.assertEqual(manifest.member_slugs(m), {"Azure/a", "Azure/b"})

    def test_rejects_missing_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"window": {"from": "x", "to": "y"},
                                "repos": [{"owner": "o", "repo": "r"}]})
            with self.assertRaisesRegex(ValueError, "project"):
                manifest.load_manifest(path)

    def test_rejects_missing_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"project": "p", "repos": [{"owner": "o", "repo": "r"}]})
            with self.assertRaisesRegex(ValueError, "window"):
                manifest.load_manifest(path)

    def test_rejects_empty_repos(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"project": "p",
                                "window": {"from": "x", "to": "y"}, "repos": []})
            with self.assertRaisesRegex(ValueError, "repos"):
                manifest.load_manifest(path)

    def test_rejects_member_without_owner_or_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"project": "p", "window": {"from": "x", "to": "y"},
                                "repos": [{"owner": "o"}]})
            with self.assertRaisesRegex(ValueError, "owner"):
                manifest.load_manifest(path)

    def test_rejects_non_object_toplevel(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, [{"project": "p"}])  # top-level array
            with self.assertRaisesRegex(ValueError, "object"):
                manifest.load_manifest(path)

    def test_rejects_non_object_repo_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"project": "p", "window": {"from": "x", "to": "y"},
                                "repos": ["Azure/a"]})  # string, not object
            with self.assertRaisesRegex(ValueError, "object"):
                manifest.load_manifest(path)

    def test_rejects_slash_in_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"project": "org/proj",
                                "window": {"from": "x", "to": "y"},
                                "repos": [{"owner": "o", "repo": "r"}]})
            with self.assertRaisesRegex(ValueError, "project"):
                manifest.load_manifest(path)

    def test_rejects_slash_in_owner_or_repo(self):
        for entry in ({"owner": "Az/ure", "repo": "r"},
                      {"owner": "o", "repo": "re/po"}):
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as tmp:
                path = _write(tmp, {"project": "p",
                                    "window": {"from": "x", "to": "y"},
                                    "repos": [entry]})
                with self.assertRaisesRegex(ValueError, "'/'"):
                    manifest.load_manifest(path)

    def test_rejects_non_string_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"project": "p", "window": {"from": "x", "to": "y"},
                                "repos": [{"owner": "o", "repo": "r",
                                           "registry": 123}]})
            with self.assertRaisesRegex(ValueError, "registry"):
                manifest.load_manifest(path)

    def test_member_slugs_empty(self):
        self.assertEqual(manifest.member_slugs({"repos": []}), set())


if __name__ == "__main__":
    unittest.main()
