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


if __name__ == "__main__":
    unittest.main()
