import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import manifest as manifest_mod  # noqa: E402
import manifest_from_index as mfi  # noqa: E402
from _quiet import quiet  # noqa: E402

# A realistic slice of the AVM TerraformResourceModules.csv schema (the verbatim
# header) plus one pattern row and an unusable row (blank RepoURL -> dropped).
HEADER = ("ProviderNamespace,ResourceType,ModuleDisplayName,AlternativeNames,"
          "ModuleName,ParentModule,ModuleStatus,RepoURL,PublicRegistryReference,"
          "PrimaryModuleOwnerGHHandle,PrimaryModuleOwnerDisplayName,"
          "SecondaryModuleOwnerGHHandle,SecondaryModuleOwnerDisplayName,"
          "Description,Comments,FirstPublishedIn")


def _row(ns, rtype, name, status, repo, reg):
    return ("{ns},{rt},Disp,,{name},n/a,{st},{repo},{reg},,,,,desc,,".format(
        ns=ns, rt=rtype, name=name, st=status, repo=repo, reg=reg))


FIXTURE = "\n".join([
    HEADER,
    _row("Microsoft.Network", "virtualNetworks", "avm-res-network-virtualnetwork",
         "Available 🟢",
         "https://github.com/Azure/terraform-azurerm-avm-res-network-virtualnetwork",
         "https://registry.terraform.io/modules/Azure/avm-res-network-virtualnetwork/azurerm/latest"),
    _row("Microsoft.OperationalInsights", "workspaces",
         "avm-res-operationalinsights-workspace", "Available 🟢",
         "https://github.com/Azure/terraform-azurerm-avm-res-operationalinsights-workspace",
         "https://registry.terraform.io/modules/Azure/avm-res-operationalinsights-workspace/azurerm/latest"),
    _row("Microsoft.AAD", "domainServices", "avm-res-aad-domainservice", "Proposed 🔴",
         "https://github.com/Azure/terraform-azurerm-avm-res-aad-domainservice",
         "https://registry.terraform.io/modules/Azure/avm-res-aad-domainservice/azurerm/latest"),
    # pattern module
    _row("Microsoft.Cloud", "aiml", "avm-ptn-aiml-landing-zone", "Available 🟢",
         "https://github.com/Azure/terraform-azurerm-avm-ptn-aiml-landing-zone",
         "https://registry.terraform.io/modules/Azure/avm-ptn-aiml-landing-zone/azurerm/latest"),
    # unusable: no RepoURL -> dropped
    _row("Microsoft.Nothing", "none", "avm-res-nothing", "Proposed 🔴", "", ""),
])


class TestParsers(unittest.TestCase):
    def test_owner_repo_from_url(self):
        self.assertEqual(
            mfi._owner_repo_from_url("https://github.com/Azure/terraform-azurerm-avm-res-x"),
            ("Azure", "terraform-azurerm-avm-res-x"))
        self.assertEqual(
            mfi._owner_repo_from_url("https://github.com/Azure/repo/"),
            ("Azure", "repo"))
        self.assertIsNone(mfi._owner_repo_from_url(""))
        self.assertIsNone(mfi._owner_repo_from_url("https://example.com/x"))

    def test_registry_from_reference(self):
        self.assertEqual(
            mfi._registry_from_reference(
                "https://registry.terraform.io/modules/Azure/avm-res-x/azurerm/latest"),
            "Azure/avm-res-x/azurerm")
        # bare triple (no URL) is accepted
        self.assertEqual(mfi._registry_from_reference("Azure/avm-res-x/azurerm"),
                         "Azure/avm-res-x/azurerm")
        self.assertIsNone(mfi._registry_from_reference(""))
        self.assertIsNone(mfi._registry_from_reference("https://example.com/foo"))

    def test_kind_of(self):
        self.assertEqual(mfi._kind_of("avm-res-network-virtualnetwork"), "res")
        self.assertEqual(mfi._kind_of("avm-ptn-aiml-landing-zone"), "ptn")
        self.assertEqual(mfi._kind_of("avm-utl-types"), "utl")
        self.assertIsNone(mfi._kind_of("not-avm"))
        self.assertIsNone(mfi._kind_of(None))


class TestParseIndex(unittest.TestCase):
    def test_parses_rows_and_drops_unusable(self):
        rows = mfi.parse_index(FIXTURE)
        self.assertEqual(len(rows), 4)  # the blank-RepoURL row is dropped
        vnet = next(r for r in rows if r["module"] == "avm-res-network-virtualnetwork")
        self.assertEqual(vnet["owner"], "Azure")
        self.assertEqual(vnet["repo"], "terraform-azurerm-avm-res-network-virtualnetwork")
        self.assertEqual(vnet["registry"], "Azure/avm-res-network-virtualnetwork/azurerm")
        self.assertEqual(vnet["kind"], "res")
        self.assertIn("Available", vnet["status"])

    def test_tolerates_bom(self):
        rows = mfi.parse_index("\ufeff" + FIXTURE)
        self.assertEqual(len(rows), 4)


class TestBuildManifest(unittest.TestCase):
    def setUp(self):
        self.rows = mfi.parse_index(FIXTURE)

    def test_status_and_kind_filters(self):
        man = mfi.build_manifest(self.rows, "p", "2026-03-16", "2026-03-22",
                                 kinds=["res"], statuses=["available"])
        repos = [m["repo"] for m in man["repos"]]
        # only Available res modules (aad is Proposed -> out; ptn -> out)
        self.assertEqual(repos, [
            "terraform-azurerm-avm-res-network-virtualnetwork",
            "terraform-azurerm-avm-res-operationalinsights-workspace"])
        self.assertEqual(man["window"], {"from": "2026-03-16", "to": "2026-03-22"})
        self.assertTrue(all(m["registry"] for m in man["repos"]))

    def test_name_contains_any_match(self):
        man = mfi.build_manifest(self.rows, "p", "2026-03-01", "2026-03-31",
                                 name_contains=["virtualnetwork", "landing-zone"])
        self.assertEqual({m["repo"] for m in man["repos"]}, {
            "terraform-azurerm-avm-res-network-virtualnetwork",
            "terraform-azurerm-avm-ptn-aiml-landing-zone"})

    def test_include_bypasses_filters_exclude_drops(self):
        # status filter would drop the Proposed aad module; --include keeps it.
        man = mfi.build_manifest(
            self.rows, "p", "2026-03-01", "2026-03-31", statuses=["available"],
            include=["Azure/terraform-azurerm-avm-res-aad-domainservice"],
            exclude=["Azure/terraform-azurerm-avm-res-operationalinsights-workspace"])
        repos = {m["repo"] for m in man["repos"]}
        self.assertIn("terraform-azurerm-avm-res-aad-domainservice", repos)        # forced in
        self.assertNotIn("terraform-azurerm-avm-res-operationalinsights-workspace", repos)  # dropped

    def test_limit_and_sorted_dedup(self):
        man = mfi.build_manifest(self.rows + self.rows, "p", "2026-03-01", "2026-03-31", limit=2)
        self.assertEqual(len(man["repos"]), 2)                  # dedup + cap
        slugs = ["{}/{}".format(m["owner"], m["repo"]) for m in man["repos"]]
        self.assertEqual(slugs, sorted(slugs))                  # deterministic order

    def test_output_roundtrips_through_load_manifest(self):
        man = mfi.build_manifest(self.rows, "avm-tf-aiml-lz", "2026-03-16",
                                 "2026-03-22", kinds=["res", "ptn"],
                                 statuses=["available"])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "manifest.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(man, fh)
            loaded = manifest_mod.load_manifest(path)             # the real validator
        self.assertEqual(loaded["project"], "avm-tf-aiml-lz")
        self.assertEqual((loaded["from"], loaded["to"]), ("2026-03-16", "2026-03-22"))
        self.assertEqual(
            manifest_mod.member_slugs(loaded),
            {"Azure/terraform-azurerm-avm-res-network-virtualnetwork",
             "Azure/terraform-azurerm-avm-res-operationalinsights-workspace",
             "Azure/terraform-azurerm-avm-ptn-aiml-landing-zone"})


class TestCli(unittest.TestCase):
    def setUp(self):
        quiet(self)

    def test_main_reads_file_writes_out(self):
        with tempfile.TemporaryDirectory() as d:
            idx = os.path.join(d, "index.csv")
            out = os.path.join(d, "manifest.json")
            with open(idx, "w", encoding="utf-8") as fh:
                fh.write(FIXTURE)
            rc = mfi.main(["--index", idx, "--project", "proj",
                           "--from", "2026-03-16", "--to", "2026-03-22",
                           "--kind", "res", "--status", "available", "--out", out])
            self.assertEqual(rc, 0)
            with open(out, encoding="utf-8") as fh:
                man = json.load(fh)
            self.assertEqual(man["project"], "proj")
            self.assertEqual(len(man["repos"]), 2)

    def test_main_no_match_returns_1(self):
        with tempfile.TemporaryDirectory() as d:
            idx = os.path.join(d, "index.csv")
            with open(idx, "w", encoding="utf-8") as fh:
                fh.write(FIXTURE)
            rc = mfi.main(["--index", idx, "--project", "p", "--from", "2026-03-01",
                           "--to", "2026-03-31", "--name-contains", "nonexistent-zzz"])
            self.assertEqual(rc, 1)

    def test_main_needs_a_source(self):
        self.assertEqual(
            mfi.main(["--project", "p", "--from", "2026-03-01", "--to", "2026-03-31"]), 2)

    def test_main_rejects_slash_project(self):
        # the manifest contract forbids '/' in project; enforce before emitting.
        rc = mfi.main(["--avm", "res", "--project", "a/b",
                       "--from", "2026-03-01", "--to", "2026-03-31"])
        self.assertEqual(rc, 2)

    def test_main_read_failure_returns_2(self):
        # a missing index file is an OSError; the CLI returns 2, not a traceback.
        rc = mfi.main(["--index", "/no/such/index.csv", "--project", "p",
                       "--from", "2026-03-01", "--to", "2026-03-31"])
        self.assertEqual(rc, 2)

    def test_read_index_sets_user_agent(self):
        captured = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"ModuleName,RepoURL\n"

        def fake(req, timeout=None):
            captured["req"] = req
            captured["timeout"] = timeout
            return _Resp()

        mfi._read_index("https://example.com/x.csv", opener=fake)
        self.assertEqual(captured["req"].get_header("User-agent"), "activity-overview")
        # issue #57: the index fetch is bounded (was: no timeout at all).
        self.assertEqual(captured["timeout"], 30)


def _messy_fixture():
    """Index rows that used to leak into the manifest unnormalized (#57)."""
    return "\n".join([
        HEADER,
        _row("Microsoft.KeyVault", "vaults", "avm-res-keyvault-vault", "Available",
             "https://github.com/Azure/terraform-azurerm-avm-res-keyvault-vault.git",
             "Azure/avm-res-keyvault-vault/azurerm"),
        _row("Microsoft.Web", "sites", "avm-res-web-site", "Available",
             "https://github.com/Azure/terraform-azurerm-avm-res-web-site?tab=readme",
             "Azure/avm-res-web-site/azurerm"),
        _row("Microsoft.Sql", "servers", "avm-res-sql-server", "Available",
             "https://github.com/Azure/terraform-azurerm-avm-res-sql-server#readme",
             "Azure/avm-res-sql-server/azurerm"),
        # case variant of the keyvault repo -> same member
        _row("Microsoft.KeyVault", "vaults", "avm-res-keyvault-vault", "Available",
             "https://github.com/azure/terraform-azurerm-avm-res-keyvault-vault",
             "azure/avm-res-keyvault-vault/azurerm"),
        # a name GitHub cannot host -> unusable row, dropped
        _row("Microsoft.Bad", "bad", "avm-res-bad", "Available",
             "https://github.com/Azure/bad name", ""),
    ])


class TestIndexEdgeCases(unittest.TestCase):
    """Issue #57: manifest_from_index edge cases."""

    def test_repo_url_strips_dot_git(self):
        self.assertEqual(
            mfi._owner_repo_from_url("https://github.com/Azure/terraform-x-vault.git"),
            ("Azure", "terraform-x-vault"))

    def test_repo_url_strips_query(self):
        self.assertEqual(
            mfi._owner_repo_from_url("https://github.com/Azure/q?tab=readme"),
            ("Azure", "q"))

    def test_repo_url_strips_fragment(self):
        self.assertEqual(
            mfi._owner_repo_from_url("https://github.com/Azure/q#readme"),
            ("Azure", "q"))

    def test_repo_url_with_invalid_name_is_unusable(self):
        self.assertIsNone(mfi._owner_repo_from_url("https://github.com/Azure/a b"))
        self.assertIsNone(mfi._owner_repo_from_url("https://github.com/-x/r"))

    def test_case_variants_dedupe_to_one_member(self):
        rows = mfi.parse_index(_messy_fixture())
        man = mfi.build_manifest(rows, "p", "2026-03-01", "2026-03-31")
        slugs = [(m["owner"], m["repo"]) for m in man["repos"]]
        kv = [s for s in slugs if s[1] == "terraform-azurerm-avm-res-keyvault-vault"]
        self.assertEqual(kv, [("Azure", "terraform-azurerm-avm-res-keyvault-vault")])
        self.assertEqual(len(slugs), 3)

    def test_exclude_is_case_insensitive(self):
        rows = mfi.parse_index(FIXTURE)
        man = mfi.build_manifest(
            rows, "p", "2026-03-01", "2026-03-31",
            exclude=["azure/TERRAFORM-azurerm-avm-res-aad-domainservice"])
        self.assertNotIn("terraform-azurerm-avm-res-aad-domainservice",
                         {m["repo"] for m in man["repos"]})

    def test_build_rejects_bad_limit(self):
        rows = mfi.parse_index(FIXTURE)
        for bad in (0, -1):
            with self.subTest(limit=bad):
                with self.assertRaisesRegex(ValueError, "limit"):
                    mfi.build_manifest(rows, "p", "2026-03-01", "2026-03-31",
                                       limit=bad)

    def test_build_rejects_duplicate_registry(self):
        rows = [
            {"module": "m1", "kind": "res", "status": "Available", "owner": "Azure",
             "repo": "a", "registry": "Azure/x/azurerm"},
            {"module": "m2", "kind": "res", "status": "Available", "owner": "Azure",
             "repo": "b", "registry": "Azure/x/azurerm"},
        ]
        with self.assertRaisesRegex(ValueError, "duplicate registry"):
            mfi.build_manifest(rows, "p", "2026-03-01", "2026-03-31")

    def _main(self, *extra, text=FIXTURE):
        with tempfile.TemporaryDirectory() as d:
            idx = os.path.join(d, "index.csv")
            with open(idx, "w", encoding="utf-8") as fh:
                fh.write(text)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = mfi.main(["--index", idx, "--project", "p", *extra])
        return rc, out.getvalue(), err.getvalue()

    def test_main_rejects_negative_limit(self):
        rc, out, err = self._main("--from", "2026-03-01", "--to", "2026-03-31",
                                  "--limit", "-1")
        self.assertEqual(rc, 2)
        self.assertIn("--limit", err)
        self.assertEqual(out, "")

    def test_main_rejects_invalid_dates(self):
        rc, out, err = self._main("--from", "2026-13-99", "--to", "nonsense")
        self.assertEqual(rc, 2)
        self.assertIn("--from", err)
        self.assertEqual(out, "")

    def test_main_rejects_invalid_to(self):
        rc, _, err = self._main("--from", "2026-03-01", "--to", "nonsense")
        self.assertEqual(rc, 2)
        self.assertIn("--to", err)

    def test_main_rejects_inverted_dates(self):
        rc, _, err = self._main("--from", "2026-03-31", "--to", "2026-03-01")
        self.assertEqual(rc, 2)
        self.assertIn("after", err)

    def test_main_warns_on_unknown_include(self):
        rc, out, err = self._main("--from", "2026-03-01", "--to", "2026-03-31",
                                  "--kind", "ptn", "--include", "Azure/no-such-repo")
        self.assertEqual(rc, 0)
        self.assertIn("Azure/no-such-repo", err)
        self.assertIn("not in the index", err)
        self.assertEqual(len(json.loads(out)["repos"]), 1)

    def test_main_known_include_does_not_warn(self):
        rc, _, err = self._main(
            "--from", "2026-03-01", "--to", "2026-03-31", "--kind", "ptn",
            "--include", "azure/terraform-azurerm-avm-res-aad-domainservice")
        self.assertEqual(rc, 0)
        self.assertNotIn("not in the index", err)

    def test_generator_output_always_passes_load_manifest(self):
        cases = [
            (FIXTURE, []), (FIXTURE, ["--status", "available"]),
            (FIXTURE, ["--limit", "1"]), (_messy_fixture(), []),
            (FIXTURE + "\n" + _messy_fixture().split("\n", 1)[1], []),
        ]
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "ci", "constellation-index.csv"),
                  encoding="utf-8") as fh:
            cases.append((fh.read(), []))
        for text, extra in cases:
            with self.subTest(extra=extra, rows=text.count("\n")):
                rc, out, _ = self._main("--from", "2026-03-01", "--to", "2026-03-31",
                                        *extra, text=text)
                self.assertEqual(rc, 0)
                with tempfile.TemporaryDirectory() as d:
                    path = os.path.join(d, "m.json")
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(out)
                    loaded = manifest_mod.load_manifest(path)
                self.assertTrue(loaded["repos"])


if __name__ == "__main__":
    unittest.main()
