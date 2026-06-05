import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import graphstore  # noqa: E402
import gather  # noqa: E402
import digest  # noqa: E402


def _seed_two_member_store(conn):
    """Member A (Azure/mod-a): PR #10 merged, closes mod-b#3 (cross-repo, via
    Part 1 fold). Member B (Azure/mod-b): issue #3 closed. Both in window 2026-01."""
    graphstore.init_schema(conn)
    bundle_a = {
        "meta": {"owner": "Azure", "repo": "mod-a", "from": "2026-01-01",
                 "to": "2026-01-31", "base_branch": "main"},
        "prs": [{"number": 10, "url": "uA/10", "state": "closed", "merged": True,
                 "base": "main", "head": "f10",
                 "merged_at": "2026-01-10T00:00:00Z",
                 "created_at": "2026-01-05T00:00:00Z",
                 "closed_at": "2026-01-10T00:00:00Z",
                 "closes": [], "crossref_issues": [],
                 "title": "feat: thing", "body": "Closes Azure/mod-b#3"}],
        "issues": [], "commits": [], "code_events": [],
        "milestones": [], "releases": [], "code_graph": {"areas": []},
    }
    bundle_b = {
        "meta": {"owner": "Azure", "repo": "mod-b", "from": "2026-01-01",
                 "to": "2026-01-31", "base_branch": "main"},
        "prs": [], "issues": [{"number": 3, "url": "uB/3", "state": "closed",
                               "closed_at": "2026-01-08T00:00:00Z",
                               "updated_at": "2026-01-08T00:00:00Z"}],
        "commits": [], "code_events": [],
        "milestones": [], "releases": [], "code_graph": {"areas": []},
    }
    members = {"Azure/mod-a", "Azure/mod-b"}
    gather.fold_bundle(conn, bundle_a, project="proj", repo="Azure/mod-a",
                       members=members)
    gather.fold_bundle(conn, bundle_b, project="proj", repo="Azure/mod-b",
                       members=members)


class TestMemberBundles(unittest.TestCase):
    def test_one_enriched_bundle_per_member(self):
        conn = graphstore.open_store(":memory:")
        _seed_two_member_store(conn)
        members = digest.member_bundles(
            conn, "proj", ["Azure/mod-a", "Azure/mod-b"],
            "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
        self.assertEqual([m["repo"] for m in members],
                         ["Azure/mod-a", "Azure/mod-b"])
        self.assertIn("trains", members[0]["bundle"])
        self.assertIn("buckets", members[0]["bundle"])
        self.assertEqual([p["number"] for p in members[0]["bundle"]["prs"]], [10])
        self.assertEqual(members[1]["bundle"]["prs"], [])


class TestSpineComponents(unittest.TestCase):
    def test_cross_repo_edge_unifies_two_members(self):
        conn = graphstore.open_store(":memory:")
        _seed_two_member_store(conn)
        comps = digest.spine_components(
            conn, "proj", ["Azure/mod-a", "Azure/mod-b"],
            "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
        # exactly one component, containing A's PR and B's issue
        self.assertEqual(len(comps), 1)
        self.assertEqual(comps[0],
                         frozenset({"proj/Azure/mod-a#pr-10",
                                    "proj/Azure/mod-b#issue-3"}))

    def test_unconnected_socials_are_separate_components(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        graphstore.upsert_nodes(conn, [
            ("proj/Azure/a#pr-1", "proj", "Azure/a", "social", "2026-01-01T00:00:00Z", {}, None),
            ("proj/Azure/b#pr-9", "proj", "Azure/b", "social", "2026-01-02T00:00:00Z", {}, None),
        ])
        comps = digest.spine_components(
            conn, "proj", ["Azure/a", "Azure/b"],
            "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
        self.assertEqual({frozenset(c) for c in comps},
                         {frozenset({"proj/Azure/a#pr-1"}),
                          frozenset({"proj/Azure/b#pr-9"})})

    def test_three_anchor_chain_is_one_component(self):
        # PR#1 and PR#3 both close issue#2 -> issue-2 bridges them into ONE
        # component of three social anchors. Exercises `seen` dedup (issue-2 and
        # pr-3 are absorbed by pr-1's traversal before they are seen as seeds).
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        bundle = {
            "meta": {"owner": "Azure", "repo": "mod", "from": "2026-01-01",
                     "to": "2026-01-31", "base_branch": "main"},
            "prs": [
                {"number": 1, "url": "u/1", "state": "closed", "merged": True,
                 "base": "main", "head": "h1", "merged_at": "2026-01-05T00:00:00Z",
                 "created_at": "2026-01-02T00:00:00Z",
                 "closed_at": "2026-01-05T00:00:00Z",
                 "closes": [2], "crossref_issues": [], "title": "feat", "body": ""},
                {"number": 3, "url": "u/3", "state": "closed", "merged": True,
                 "base": "main", "head": "h3", "merged_at": "2026-01-06T00:00:00Z",
                 "created_at": "2026-01-03T00:00:00Z",
                 "closed_at": "2026-01-06T00:00:00Z",
                 "closes": [2], "crossref_issues": [], "title": "feat", "body": ""},
            ],
            "issues": [{"number": 2, "url": "u/2", "state": "closed",
                        "closed_at": "2026-01-04T00:00:00Z",
                        "updated_at": "2026-01-04T00:00:00Z"}],
            "commits": [], "code_events": [], "milestones": [], "releases": [],
            "code_graph": {"areas": []},
        }
        gather.fold_bundle(conn, bundle, project="proj", repo="Azure/mod",
                           members={"Azure/mod"})
        comps = digest.spine_components(
            conn, "proj", ["Azure/mod"],
            "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
        self.assertEqual(len(comps), 1)
        self.assertEqual(comps[0], frozenset({
            "proj/Azure/mod#pr-1", "proj/Azure/mod#issue-2", "proj/Azure/mod#pr-3"}))

    def test_two_separate_multi_node_components_are_isolated(self):
        # Two independent PR->issue pairs -> two disjoint 2-node components.
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        def _pair(pr_n, iss_n):
            return {
                "meta": {"owner": "Azure", "repo": "mod", "from": "2026-01-01",
                         "to": "2026-01-31", "base_branch": "main"},
                "prs": [{"number": pr_n, "url": "u/{}".format(pr_n),
                         "state": "closed", "merged": True, "base": "main",
                         "head": "h{}".format(pr_n),
                         "merged_at": "2026-01-05T00:00:00Z",
                         "created_at": "2026-01-02T00:00:00Z",
                         "closed_at": "2026-01-05T00:00:00Z",
                         "closes": [iss_n], "crossref_issues": [],
                         "title": "feat", "body": ""}],
                "issues": [{"number": iss_n, "url": "u/i{}".format(iss_n),
                            "state": "closed", "closed_at": "2026-01-04T00:00:00Z",
                            "updated_at": "2026-01-04T00:00:00Z"}],
                "commits": [], "code_events": [], "milestones": [],
                "releases": [], "code_graph": {"areas": []},
            }
        gather.fold_bundle(conn, _pair(1, 2), project="proj", repo="Azure/mod",
                           members={"Azure/mod"})
        gather.fold_bundle(conn, _pair(5, 6), project="proj", repo="Azure/mod",
                           members={"Azure/mod"})
        comps = digest.spine_components(
            conn, "proj", ["Azure/mod"],
            "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
        self.assertEqual({frozenset(c) for c in comps}, {
            frozenset({"proj/Azure/mod#pr-1", "proj/Azure/mod#issue-2"}),
            frozenset({"proj/Azure/mod#pr-5", "proj/Azure/mod#issue-6"})})


class TestBuildProjectTrains(unittest.TestCase):
    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        _seed_two_member_store(self.conn)
        self.frm, self.to = "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z"
        self.members = digest.member_bundles(
            self.conn, "proj", ["Azure/mod-a", "Azure/mod-b"], self.frm, self.to)
        self.comps = digest.spine_components(
            self.conn, "proj", ["Azure/mod-a", "Azure/mod-b"], self.frm, self.to)

    def test_cross_repo_train_is_single_and_spans_repos(self):
        trains = digest.build_project_trains(self.members, self.comps, "proj")
        self.assertEqual(len(trains), 1)
        t = trains[0]
        self.assertEqual(set(t["repos"]), {"Azure/mod-a", "Azure/mod-b"})
        self.assertIn("proj/Azure/mod-a#pr-10", t["prs"])
        self.assertIn("proj/Azure/mod-b#issue-3", t["issues"])
        self.assertEqual(t["outcome"], "shipped")

    def test_single_repo_train_preserved_with_qualified_ids(self):
        conn = graphstore.open_store(":memory:")
        graphstore.init_schema(conn)
        b = {"meta": {"owner": "Azure", "repo": "solo", "from": "2026-01-01",
                      "to": "2026-01-31", "base_branch": "main"},
             "prs": [{"number": 7, "url": "u/7", "state": "closed", "merged": True,
                      "base": "main", "head": "h7",
                      "merged_at": "2026-01-09T00:00:00Z",
                      "created_at": "2026-01-02T00:00:00Z",
                      "closed_at": "2026-01-09T00:00:00Z",
                      "closes": [], "crossref_issues": [], "title": "fix: x",
                      "body": ""}],
             "issues": [], "commits": [], "code_events": [],
             "milestones": [], "releases": [], "code_graph": {"areas": []}}
        gather.fold_bundle(conn, b, project="proj", repo="Azure/solo",
                           members={"Azure/solo"})
        frm, to = "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z"
        members = digest.member_bundles(conn, "proj", ["Azure/solo"], frm, to)
        comps = digest.spine_components(conn, "proj", ["Azure/solo"], frm, to)
        trains = digest.build_project_trains(members, comps, "proj")
        self.assertEqual(len(trains), 1)
        self.assertEqual(trains[0]["repos"], ["Azure/solo"])
        self.assertEqual(trains[0]["prs"], ["proj/Azure/solo#pr-7"])


if __name__ == "__main__":
    unittest.main()
