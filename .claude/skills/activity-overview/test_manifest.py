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
            path = _write(tmp, {"window": {"from": "2026-03-01", "to": "2026-03-31"},
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
                                "window": {"from": "2026-03-01", "to": "2026-03-31"}, "repos": []})
            with self.assertRaisesRegex(ValueError, "repos"):
                manifest.load_manifest(path)

    def test_rejects_member_without_owner_or_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"project": "p", "window": {"from": "2026-03-01", "to": "2026-03-31"},
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
            path = _write(tmp, {"project": "p", "window": {"from": "2026-03-01", "to": "2026-03-31"},
                                "repos": ["Azure/a"]})  # string, not object
            with self.assertRaisesRegex(ValueError, "object"):
                manifest.load_manifest(path)

    def test_rejects_slash_in_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"project": "org/proj",
                                "window": {"from": "2026-03-01", "to": "2026-03-31"},
                                "repos": [{"owner": "o", "repo": "r"}]})
            with self.assertRaisesRegex(ValueError, "project"):
                manifest.load_manifest(path)

    def test_rejects_slash_in_owner_or_repo(self):
        for entry in ({"owner": "Az/ure", "repo": "r"},
                      {"owner": "o", "repo": "re/po"}):
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as tmp:
                path = _write(tmp, {"project": "p",
                                    "window": {"from": "2026-03-01", "to": "2026-03-31"},
                                    "repos": [entry]})
                with self.assertRaisesRegex(ValueError, "'/'"):
                    manifest.load_manifest(path)

    def test_rejects_non_string_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, {"project": "p", "window": {"from": "2026-03-01", "to": "2026-03-31"},
                                "repos": [{"owner": "o", "repo": "r",
                                           "registry": 123}]})
            with self.assertRaisesRegex(ValueError, "registry"):
                manifest.load_manifest(path)

    def test_member_slugs_empty(self):
        self.assertEqual(manifest.member_slugs({"repos": []}), set())


_WIN = {"from": "2026-03-01", "to": "2026-03-31"}


def _man(repos=None, window=None, project="p"):
    return {"project": project, "window": _WIN if window is None else window,
            "repos": [{"owner": "o", "repo": "r"}] if repos is None else repos}


class TestManifestValidation(unittest.TestCase):
    """Issue #57: inputs load_manifest used to accept (or crash on with a raw
    TypeError). Every rejection is a ValueError naming the offending field."""

    def assertRejects(self, obj, pattern):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, obj)
            with self.assertRaisesRegex(ValueError, pattern):
                manifest.load_manifest(path)

    def test_rejects_duplicate_member(self):
        self.assertRejects(_man([{"owner": "o", "repo": "a"},
                                 {"owner": "o", "repo": "a"}]), "duplicate member")

    def test_rejects_case_variant_duplicate_member(self):
        self.assertRejects(_man([{"owner": "o", "repo": "a"},
                                 {"owner": "O", "repo": "A"}]),
                           r"duplicate member 'O/A'.*'o/a'")

    def test_rejects_non_iso_from(self):
        self.assertRejects(_man(window={"from": "31/03/2026", "to": "2026-03-31"}),
                           r"window\.from.*YYYY-MM-DD.*31/03/2026")

    def test_rejects_compact_iso_date(self):
        # Python 3.11 fromisoformat accepts '20260301'; the contract does not.
        self.assertRejects(_man(window={"from": "20260301", "to": "2026-03-31"}),
                           r"window\.from.*YYYY-MM-DD")

    def test_rejects_impossible_date(self):
        self.assertRejects(_man(window={"from": "2026-02-30", "to": "2026-03-31"}),
                           r"window\.from")

    def test_rejects_inverted_window(self):
        self.assertRejects(_man(window={"from": "2026-03-31", "to": "2026-03-01"}),
                           r"window\.from 2026-03-31 is after window\.to 2026-03-01")

    def test_rejects_wrong_format_and_inverted_window(self):
        # the exact pair from the issue: the format error is reported first.
        self.assertRejects(_man(window={"from": "31/03/2026", "to": "2026-03-01"}),
                           r"window\.from.*YYYY-MM-DD")

    def test_rejects_integer_window_values(self):
        self.assertRejects(_man(window={"from": 20260301, "to": 20260331}),
                           r"window\.from.*must be a string")

    def test_rejects_url_metacharacters_in_repo(self):
        self.assertRejects(_man([{"owner": "o", "repo": "b?per_page=1#"}]),
                           r"repos\[0\].*'repo'.*b\?per_page")

    def test_rejects_whitespace_in_repo(self):
        self.assertRejects(_man([{"owner": "o", "repo": " a b "}]), r"'repo'")

    def test_rejects_reserved_repo_names(self):
        for name in (".", ".."):
            with self.subTest(name=name):
                self.assertRejects(_man([{"owner": "o", "repo": name}]),
                                   r"'repo'.*reserved")

    def test_rejects_owner_starting_with_hyphen(self):
        self.assertRejects(_man([{"owner": "-x", "repo": "r"}]), r"'owner'.*-x")

    def test_rejects_dotdot_owner(self):
        self.assertRejects(_man([{"owner": "..", "repo": "r"}]), r"'owner'")

    def test_rejects_owner_with_dot_or_unsafe_characters(self):
        for owner in ("a.b", "a b", "a?b", "a#b", "a%2Fb"):
            with self.subTest(owner=owner):
                self.assertRejects(_man([{"owner": owner, "repo": "r"}]), r"'owner'")

    def test_accepts_legacy_and_enterprise_owner_names(self):
        # Real GitHub names the sign-up rule no longer allows: legacy accounts
        # with "--", Enterprise Managed User logins with "_".
        for owner in ("a--b", "name_corp", "trailing-"):
            with self.subTest(owner=owner):
                self.assertEqual(manifest.check_owner(owner), owner)

    def test_rejects_integer_owner_with_value_error(self):
        self.assertRejects(_man([{"owner": 123, "repo": "r"}]),
                           r"'owner' must be a string")

    def test_rejects_integer_repo_with_value_error(self):
        self.assertRejects(_man([{"owner": "o", "repo": 7}]),
                           r"'repo' must be a string")

    def test_rejects_integer_project_with_value_error(self):
        self.assertRejects(_man(project=42), r"'project' must be a string")

    def test_rejects_hash_in_project(self):
        # '#' splits a node id's scope from its local id (STORE.md grammar).
        self.assertRejects(_man(project="p#1"), r"'project'.*'#'")

    def test_rejects_duplicate_registry(self):
        reg = "Azure/avm-res-x/azurerm"
        self.assertRejects(_man([{"owner": "o", "repo": "a", "registry": reg},
                                 {"owner": "o", "repo": "b", "registry": reg}]),
                           r"duplicate registry .*'o/b'.*'o/a'")

    def test_rejects_case_variant_duplicate_registry(self):
        self.assertRejects(_man([{"owner": "o", "repo": "a",
                                  "registry": "Azure/avm-res-x/azurerm"},
                                 {"owner": "o", "repo": "b",
                                  "registry": "azure/avm-res-x/azurerm"}]),
                           r"duplicate registry")

    def test_rejects_empty_registry(self):
        self.assertRejects(_man([{"owner": "o", "repo": "a", "registry": ""}]),
                           r"'registry'")

    def test_accepts_valid_edge_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(tmp, _man([
                {"owner": "a-b-c", "repo": ".dotfiles"},
                {"owner": "Az9", "repo": "x_y.z-1"},
                {"owner": "o", "repo": "a", "registry": None}],
                window={"from": "2026-03-01", "to": "2026-03-01"}))
            m = manifest.load_manifest(path)
        self.assertEqual(len(m["repos"]), 3)

    def test_committed_manifests_load(self):
        here = os.path.dirname(os.path.abspath(__file__))
        for rel in ("examples/manifest.json", "samples/manifest.json"):
            with self.subTest(path=rel):
                m = manifest.load_manifest(os.path.join(here, rel))
                self.assertTrue(m["repos"])


class TestSharedValidators(unittest.TestCase):
    def test_check_date_returns_value(self):
        self.assertEqual(manifest.check_date("2026-03-01", "--from"), "2026-03-01")

    def test_check_window_inverted(self):
        with self.assertRaisesRegex(ValueError, "--from 2026-03-02 is after --to"):
            manifest.check_window("2026-03-02", "2026-03-01", "--from", "--to")

    def test_check_owner_and_repo_accept_valid(self):
        self.assertEqual(manifest.check_owner("Azure"), "Azure")
        self.assertEqual(manifest.check_repo("bicep-registry-modules"),
                         "bicep-registry-modules")

    def test_length_limits(self):
        with self.assertRaisesRegex(ValueError, "'owner'"):
            manifest.check_owner("a" * 40)
        with self.assertRaisesRegex(ValueError, "'repo'"):
            manifest.check_repo("a" * 101)


if __name__ == "__main__":
    unittest.main()


class TestFullStringValidation(unittest.TestCase):
    def test_trailing_newline_is_rejected(self):
        # `$` also matches before a final newline; the checks must use fullmatch.
        for check, value in ((manifest.check_owner, "Azure\n"),
                             (manifest.check_repo, "bicep\n"),
                             (lambda v: manifest.check_date(v, "date"),
                              "2026-05-01\n")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    check(value)

    def test_reader_window_validates_whole_bounds(self):
        ok = (("2026-05-01", "2026-05-31"), ("2026-05-01", "2026-05-01"),
              ("2026-05-01T00:00:00Z", "2026-05-31T23:59:59Z"),
              ("2026-05-01T09:00:00+02:00", "2026-05-01"))
        for a, b in ok:
            with self.subTest(a=a, b=b):
                self.assertEqual(manifest.check_reader_window(a, b), (a, b))
        bad = (("2026-05-01Tgarbage", "2026-05-31"),
               ("2026-05-01T12:00:00Z", "2026-05-01T09:00:00Z"),
               ("2026-05-31", "2026-05-01"), ("2026-02-30", "2026-03-01"),
               ("2026-05-01\n", "2026-05-31"), ("20260501", "2026-05-31"))
        for a, b in bad:
            with self.subTest(a=a, b=b):
                with self.assertRaises(ValueError):
                    manifest.check_reader_window(a, b)


class TestTicketPrefixes(unittest.TestCase):
    """Issue #74: optional `ticket_prefixes` opts a project into digest's
    ticket clustering: a list of distinct, non-empty ASCII letter/digit strings."""

    def _load(self, obj):
        with tempfile.TemporaryDirectory() as tmp:
            return manifest.load_manifest(_write(tmp, obj))

    def test_absent_defaults_to_empty_list(self):
        self.assertEqual(self._load(_man())["ticket_prefixes"], [])

    def test_valid_prefixes_kept_in_order(self):
        obj = dict(_man(), ticket_prefixes=["PLAT", "ABC", "X9"])
        self.assertEqual(self._load(obj)["ticket_prefixes"], ["PLAT", "ABC", "X9"])

    def test_empty_list_is_allowed(self):
        self.assertEqual(self._load(dict(_man(), ticket_prefixes=[]))
                         ["ticket_prefixes"], [])

    def test_rejects_bad_values(self):
        for bad in ("ABC", None, ["ABC", ""], ["AB-C"], ["ABC\n"], [7], ["A B"],
                    {"ABC": 1}, ["ABC", "ABC"], ["É"]):
            with self.subTest(bad=bad):
                with tempfile.TemporaryDirectory() as tmp:
                    path = _write(tmp, dict(_man(), ticket_prefixes=bad))
                    with self.assertRaisesRegex(ValueError,
                                                "manifest: .*ticket_prefixes"):
                        manifest.load_manifest(path)
