"""Case- and form-insensitive multi-repo matching (issue #70).

GitHub repo slugs and Terraform registry addresses compare case-insensitively.
Node ids keep the manifest's spelling; only comparison/lookup is normalised, so
any spelling resolves to the one canonical (manifest / stored) slug.

Offline: pure functions, in-memory stores, and the CLIs run in-process.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import digest  # noqa: E402
import gather  # noqa: E402
import graphstore  # noqa: E402
import manifest  # noqa: E402
import spotlight  # noqa: E402
import view  # noqa: E402
from test_digest import _seed_two_member_store  # noqa: E402


def _q(slug, local, project="proj"):
    return graphstore.qualify_id(project, slug, local)


class TestCrossRepoPrEdgesCase(unittest.TestCase):
    """Issue case 1: current repo `azure/me`, member `Azure/custom-repo`."""

    MEMBERS = {"azure/me", "Azure/custom-repo"}
    BODY = "Closes azure/Custom-Repo#7 and also closes Azure/me#3"

    def test_mixed_case_member_ref_is_a_spine_edge_not_external(self):
        pr = {"number": 1, "title": "t", "body": self.BODY}
        external, edges = gather._cross_repo_pr_edges(
            pr, "proj", "azure/me", self.MEMBERS)
        self.assertEqual(external, [])
        # The edge targets the member's CANONICAL (manifest) id space.
        self.assertEqual([e[1:3] for e in edges],
                         [(_q("Azure/custom-repo", "issue-7"), "closes")])

    def test_self_reference_with_other_casing_is_not_cross_repo(self):
        pr = {"number": 1, "title": "", "body": "closes Azure/ME#3"}
        external, edges = gather._cross_repo_pr_edges(
            pr, "proj", "azure/me", self.MEMBERS)
        self.assertEqual((external, edges), ([], []))

    def test_spellings_of_one_ref_dedupe_to_one_edge(self):
        pr = {"number": 1, "title": "",
              "body": "Closes azure/custom-repo#7, fixes AZURE/CUSTOM-REPO#7"}
        _external, edges = gather._cross_repo_pr_edges(
            pr, "proj", "azure/me", self.MEMBERS)
        self.assertEqual([e[1:3] for e in edges],
                         [(_q("Azure/custom-repo", "issue-7"), "closes")])

    def test_external_ref_keeps_its_written_spelling(self):
        pr = {"number": 1, "title": "", "body": "Closes Other/Thing#2"}
        external, edges = gather._cross_repo_pr_edges(
            pr, "proj", "azure/me", self.MEMBERS)
        self.assertEqual(edges, [])
        self.assertEqual(external,
                         [{"repo": "Other/Thing", "number": 2, "kind": "closes"}])

    def test_timeline_xref_casing_resolves_to_member(self):
        pr = {"number": 1, "title": "", "body": "",
              "timeline_xrefs": [{"owner": "AZURE", "repo": "custom-REPO",
                                  "number": 9, "kind": "cross_ref",
                                  "is_pr": True}]}
        external, edges = gather._cross_repo_pr_edges(
            pr, "proj", "azure/me", self.MEMBERS)
        self.assertEqual(external, [])
        self.assertEqual([e[1:3] for e in edges],
                         [(_q("Azure/custom-repo", "pr-9"), "cross_ref")])

    def test_fold_end_to_end(self):
        """normalize_pr (API base full_name `Azure/me`) + fold under the manifest
        spelling `azure/me`: the self-ref is a same-repo closes edge, the member
        ref a cross-repo closes edge, and nothing lands in external_refs."""
        raw = {"number": 1, "title": "t", "body": self.BODY,
               "html_url": "https://github.com/Azure/me/pull/1",
               "base": {"ref": "main", "repo": {"full_name": "Azure/me"}},
               "merged_at": "2026-01-10T00:00:00Z",
               "created_at": "2026-01-05T00:00:00Z"}
        pr = gather.normalize_pr(raw)
        bundle = {"meta": {"owner": "azure", "repo": "me", "from": "2026-01-01",
                           "to": "2026-01-31"},
                  "prs": [pr], "issues": [], "commits": [], "code_events": [],
                  "milestones": [], "releases": []}
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, bundle, project="proj", repo="azure/me",
                           members=self.MEMBERS, registry_by_slug={})
        pid = _q("azure/me", "pr-1")
        out = sorted((e["dst_id"], e["edge_type"]) for e in graphstore.get_edges(
            conn, pid, direction="out", edge_types=["closes"]))
        self.assertEqual(out, [(_q("Azure/custom-repo", "issue-7"), "closes"),
                               (_q("azure/me", "issue-3"), "closes")])
        node = graphstore.get_node(conn, pid)
        self.assertNotIn("external_refs", node["data"])


class TestQualifiedRefsDedupeCase(unittest.TestCase):
    def test_same_ref_in_two_spellings_is_one_ref(self):
        refs = gather.parse_qualified_refs(
            "Closes Azure/x#7\nSee https://github.com/azure/X/issues/7")
        self.assertEqual([(r["owner"], r["repo"], r["number"], r["kind"])
                          for r in refs], [("Azure", "x", 7, "closes")])


class TestTimelineXrefsCase(unittest.TestCase):
    def _ev(self, full_name, number):
        return {"event": "cross-referenced",
                "source": {"issue": {"number": number,
                                     "repository": {"full_name": full_name},
                                     "pull_request": None}}}

    def test_same_repo_in_other_casing_is_dropped(self):
        # manifest/--owner spelling `azure/me` vs the API's `Azure/me`
        out = gather.parse_timeline_xrefs([self._ev("Azure/me", 3)], "azure/me")
        self.assertEqual(out, [])

    def test_dedupe_is_case_insensitive(self):
        out = gather.parse_timeline_xrefs(
            [self._ev("Azure/Other", 3), self._ev("azure/other", 3)], "azure/me")
        self.assertEqual(len(out), 1)


class TestBoardLookupCase(unittest.TestCase):
    def test_board_facts_match_any_spelling(self):
        items = {("Azure/Me", 3): {"status": "Todo", "sprint_id": "IT1"},
                 ("Azure/Me", 4): {"status": None, "sprint_id": None}}
        idx = gather._board_index(items, "azure/me")
        self.assertEqual(idx, {3: {"status": "Todo", "sprint_id": "IT1"},
                               4: {"status": None, "sprint_id": None}})

    def test_other_repo_items_excluded(self):
        items = {("Azure/other", 3): {"status": "Todo", "sprint_id": None}}
        self.assertEqual(gather._board_index(items, "azure/me"), {})

    def test_spellings_merge_first_non_none_deterministically(self):
        items = {("azure/me", 3): {"status": None, "sprint_id": "IT1"},
                 ("Azure/me", 3): {"status": "Done", "sprint_id": "IT9"}}
        # sorted key order: 'Azure/me' < 'azure/me'
        self.assertEqual(gather._board_index(items, "AZURE/ME"),
                         {3: {"status": "Done", "sprint_id": "IT9"}})


class TestRegistryResolutionForms(unittest.TestCase):
    """Issue case 2: member `Azure/custom-repo` with registry `Azure/foo/azurerm`."""

    MEMBERS = {"Azure/custom-repo", "Azure/terraform-azurerm-naming"}
    REG = {"Azure/custom-repo": "Azure/foo/azurerm"}
    CUSTOM = "p/Azure/custom-repo#area-main.tf"

    def _resolve(self, src, members=None, reg=None):
        return gather.resolve_registry_member(
            src, "p", self.MEMBERS if members is None else members,
            self.REG if reg is None else reg)

    def test_exact(self):
        self.assertEqual(self._resolve("Azure/foo/azurerm"), self.CUSTOM)

    def test_submodule_suffix(self):
        self.assertEqual(self._resolve("Azure/foo/azurerm//modules/bar"),
                         self.CUSTOM)

    def test_registry_host_prefix(self):
        self.assertEqual(self._resolve("registry.terraform.io/Azure/foo/azurerm"),
                         self.CUSTOM)

    def test_lowercase_namespace(self):
        self.assertEqual(self._resolve("azure/foo/azurerm"), self.CUSTOM)

    def test_manifest_registry_written_with_host_and_case(self):
        reg = {"Azure/custom-repo": "registry.terraform.io/AZURE/Foo/AzureRM"}
        self.assertEqual(self._resolve("azure/foo/azurerm//x", reg=reg),
                         self.CUSTOM)

    def test_convention_path_mixed_case(self):
        # `Azure/Naming/AzureRM` -> Azure/terraform-azurerm-naming by convention;
        # the id uses the member's manifest spelling.
        self.assertEqual(self._resolve("Azure/Naming/AzureRM"),
                         "p/Azure/terraform-azurerm-naming#area-main.tf")

    def test_convention_with_host_submodule_and_case(self):
        self.assertEqual(
            self._resolve("registry.terraform.io/AZURE/naming/azurerm//sub"),
            "p/Azure/terraform-azurerm-naming#area-main.tf")

    def test_exact_still_wins_over_convention_across_casing(self):
        members = {"Azure/custom-kv", "azure/terraform-azurerm-kv"}
        reg = {"Azure/custom-kv": "Azure/KV/azurerm"}
        self.assertEqual(self._resolve("azure/kv/AzureRM", members, reg),
                         "p/Azure/custom-kv#area-main.tf")

    def test_non_registry_sources_still_none(self):
        for src in ("./local", "github.com/Azure/foo", "git::https://x/y.git",
                    "Hashicorp/consul/aws", ""):
            self.assertIsNone(self._resolve(src), src)

    def test_fold_emits_depends_on_for_mixed_case_source(self):
        bundle = {"meta": {"owner": "Azure", "repo": "consumer",
                           "from": "2026-01-01", "to": "2026-01-31"},
                  "prs": [], "issues": [], "commits": [], "code_events": [],
                  "milestones": [], "releases": [],
                  "code_graph": {"provider": "directory", "areas": [
                      {"id": "main.tf", "label": "main.tf", "paths": ["main.tf"],
                       "edges": [{"to": None, "kind": "module",
                                  "ref": "registry.terraform.io/azure/FOO/azurerm"
                                         "//modules/bar",
                                  "version": "1.0.0", "transitive": False,
                                  "provider": "terraform", "resolved": False}]}]}}
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, bundle, project="proj", repo="Azure/consumer",
                           members=self.MEMBERS | {"Azure/consumer"},
                           registry_by_slug=self.REG)
        deps = graphstore.get_edges(conn, "proj/Azure/consumer#area-main.tf",
                                    direction="out", edge_types=["depends_on"])
        self.assertEqual([d["dst_id"] for d in deps],
                         ["proj/Azure/custom-repo#area-main.tf"])


class TestManifestSlugIndex(unittest.TestCase):
    def test_slug_index_maps_lowercase_to_manifest_spelling(self):
        self.assertEqual(manifest.slug_index({"Azure/custom-repo", "azure/me"}),
                         {"azure/custom-repo": "Azure/custom-repo",
                          "azure/me": "azure/me"})

    def test_canonical_slug(self):
        idx = manifest.slug_index({"Azure/custom-repo"})
        self.assertEqual(manifest.canonical_slug("AZURE/Custom-Repo", idx),
                         "Azure/custom-repo")
        self.assertIsNone(manifest.canonical_slug("Azure/other", idx))


def _seed_dependents(conn):
    graphstore.init_schema(conn)
    graphstore.upsert_nodes(conn, [
        ("proj/Azure/B#area-main.tf", "proj", "Azure/B", "structure", None,
         {"id": "area-main.tf"}, None),
        ("proj/Azure/A#area-main.tf", "proj", "Azure/A", "structure", None,
         {"id": "area-main.tf"}, None),
    ])
    graphstore.upsert_edges(conn, [
        ("proj/Azure/A#area-main.tf", "proj/Azure/B#area-main.tf", "depends_on",
         None, {"cross_repo": True}),
    ])


class TestReaderRepoResolution(unittest.TestCase):
    def test_resolve_repo(self):
        conn = graphstore.open_store(":memory:")
        _seed_dependents(conn)
        self.assertEqual(graphstore.resolve_repo(conn, "proj", "azure/b"),
                         "Azure/B")
        self.assertEqual(graphstore.resolve_repo(conn, "proj", "Azure/B"),
                         "Azure/B")
        # unknown: returned as given (the caller reports it)
        self.assertEqual(graphstore.resolve_repo(conn, "proj", "azure/zz"),
                         "azure/zz")
        # projects stay case-sensitive
        self.assertEqual(graphstore.resolve_repo(conn, "PROJ", "azure/b"),
                         "azure/b")

    def test_spotlight_dependents_any_casing(self):
        conn = graphstore.open_store(":memory:")
        _seed_dependents(conn)
        res = spotlight.member_dependents(conn, "proj", "azure/b")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["focus"], "Azure/B")
        self.assertEqual(res["dependents"], ["Azure/A"])

    def test_spotlight_dependents_self_excluded_any_casing(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        graphstore.upsert_nodes(conn, [
            ("proj/Az/A#area-app", "proj", "Az/A", "structure", None,
             {"id": "area-app"}, None),
            ("proj/Az/A#area-base", "proj", "Az/A", "structure", None,
             {"id": "area-base"}, None)])
        graphstore.upsert_edges(conn, [
            ("proj/Az/A#area-app", "proj/Az/A#area-base", "depends_on", None,
             None)])
        res = spotlight.member_dependents(conn, "proj", "az/a")
        self.assertEqual(res["dependents"], [])

    def test_digest_repo_any_casing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "j.db")
            disk = graphstore.open_store(store)
            _seed_two_member_store(disk)
            disk.close()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = digest.main(["--store", store, "--project", "proj",
                                  "--repo", "azure/MOD-A",
                                  "--from", "2026-01-01T00:00:00Z",
                                  "--to", "2026-01-31T23:59:59Z"])
        self.assertEqual(rc, 0)
        got = json.loads(buf.getvalue())
        self.assertEqual(got["meta"]["repos"], ["Azure/mod-a"])
        self.assertEqual([m["repo"] for m in got["members"]], ["Azure/mod-a"])
        self.assertEqual(len(got["members"][0]["bundle"]["prs"]), 1)

    def test_view_repo_any_casing(self):
        conn = graphstore.open_store(":memory:")
        _seed_two_member_store(conn)
        self.assertEqual(view.resolve_scope(conn, "proj", "AZURE/mod-b"),
                         ("proj", "Azure/mod-b"))
        with self.assertRaises(ValueError):
            view.resolve_scope(conn, "proj", "azure/nope")
        with self.assertRaises(ValueError):   # project stays case-sensitive
            view.resolve_scope(conn, "PROJ", "Azure/mod-b")


if __name__ == "__main__":
    unittest.main()
