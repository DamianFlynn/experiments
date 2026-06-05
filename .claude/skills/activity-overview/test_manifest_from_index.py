import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import manifest as manifest_mod  # noqa: E402
import manifest_from_index as mfi  # noqa: E402

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
        man = mfi.build_manifest(self.rows, "p", "f", "t",
                                 name_contains=["virtualnetwork", "landing-zone"])
        self.assertEqual({m["repo"] for m in man["repos"]}, {
            "terraform-azurerm-avm-res-network-virtualnetwork",
            "terraform-azurerm-avm-ptn-aiml-landing-zone"})

    def test_include_bypasses_filters_exclude_drops(self):
        # status filter would drop the Proposed aad module; --include keeps it.
        man = mfi.build_manifest(
            self.rows, "p", "f", "t", statuses=["available"],
            include=["Azure/terraform-azurerm-avm-res-aad-domainservice"],
            exclude=["Azure/terraform-azurerm-avm-res-operationalinsights-workspace"])
        repos = {m["repo"] for m in man["repos"]}
        self.assertIn("terraform-azurerm-avm-res-aad-domainservice", repos)        # forced in
        self.assertNotIn("terraform-azurerm-avm-res-operationalinsights-workspace", repos)  # dropped

    def test_limit_and_sorted_dedup(self):
        man = mfi.build_manifest(self.rows + self.rows, "p", "f", "t", limit=2)
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
            rc = mfi.main(["--index", idx, "--project", "p", "--from", "f",
                           "--to", "t", "--name-contains", "nonexistent-zzz"])
            self.assertEqual(rc, 1)

    def test_main_needs_a_source(self):
        self.assertEqual(
            mfi.main(["--project", "p", "--from", "f", "--to", "t"]), 2)

    def test_main_rejects_slash_project(self):
        # the manifest contract forbids '/' in project; enforce before emitting.
        rc = mfi.main(["--avm", "res", "--project", "a/b",
                       "--from", "f", "--to", "t"])
        self.assertEqual(rc, 2)

    def test_main_read_failure_returns_2(self):
        # a missing index file is an OSError; the CLI returns 2, not a traceback.
        rc = mfi.main(["--index", "/no/such/index.csv", "--project", "p",
                       "--from", "f", "--to", "t"])
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

        def fake(req):
            captured["req"] = req
            return _Resp()

        mfi._read_index("https://example.com/x.csv", opener=fake)
        self.assertEqual(captured["req"].get_header("User-agent"), "activity-overview")


if __name__ == "__main__":
    unittest.main()
