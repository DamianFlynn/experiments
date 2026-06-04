"""Phase 8a tests: the fts_text indexing prerequisite on the fold path, and the
spotlight reader scaffold + person-impact query.

TDD-first. A crafted small bundle is folded into an in-memory store; the
person-impact JSON is asserted against an exact ordered, cited structure.
A real-data smoke runs against workspace/journey.db when present.
"""

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402
import graphstore  # noqa: E402
import spotlight  # noqa: E402

HERE = os.path.dirname(__file__)
REAL_STORE = os.path.join(HERE, "workspace", "journey.db")


def _crafted_bundle(repo="r1"):
    """A small bundle exercising every person-impact field for login 'alice':
    she authors PR 1 (which closes issue 9 -> a train), reviews PR 2, comments
    on PR 2, reports issue 9, and authors a symbol in main.bicep that is later
    removed (by anyone). Searchable text lives in titles/bodies/comments/commit
    messages so the FTS prerequisite has content."""
    return {
        "meta": {"owner": "acme", "repo": repo,
                 "from": "2026-01-01", "to": "2026-01-31"},
        "prs": [
            {"number": 1, "title": "Add firewall policy",
             "body": "Implements the firewall policy. Closes #9",
             "author": "alice", "merged_by": "bob", "merged_at": "2026-01-10T00:00:00Z",
             "created_at": "2026-01-05T00:00:00Z", "state": "closed", "merged": True,
             "closes": [9], "reviewers": ["carol"],
             "url": "https://github.com/acme/{}/pull/1".format(repo),
             "comments_list": [{"author": "dave", "body": "nice work on the policy"}]},
            {"number": 2, "title": "Refactor storage module",
             "body": "Cleans up the storage module internals.",
             "author": "bob", "merged_by": "bob", "merged_at": "2026-01-20T00:00:00Z",
             "created_at": "2026-01-15T00:00:00Z", "state": "closed", "merged": True,
             "reviewers": ["alice"],
             "url": "https://github.com/acme/{}/pull/2".format(repo),
             "comments_list": [{"author": "alice", "body": "looks good to me"}]},
        ],
        "issues": [
            {"number": 9, "title": "Firewall policy missing",
             "body": "We need a firewall policy module.",
             "author": "alice", "state": "closed",
             "closed_at": "2026-01-10T00:00:00Z", "updated_at": "2026-01-10T00:00:00Z",
             "url": "https://github.com/acme/{}/issues/9".format(repo),
             "comments_list": [{"author": "bob", "body": "agreed, this is needed"}]},
        ],
        "commits": [
            {"sha": "c0ffee1", "message": "Add firewall policy (#1)",
             "author": "alice", "date": "2026-01-08T00:00:00Z"},
        ],
        "symbol_events": [
            {"path": "main.bicep", "lang": "bicep", "subkind": "resource",
             "name": "fw", "change": "add", "commit": "c0ffee1",
             "author": "alice", "date": "2026-01-08T00:00:00Z",
             "before": None, "after": "resource fw ..."},
            {"path": "main.bicep", "lang": "bicep", "subkind": "resource",
             "name": "fw", "change": "drop", "commit": "deadbee",
             "author": "bob", "date": "2026-01-25T00:00:00Z",
             "before": "resource fw ...", "after": None},
        ],
    }


class TestFtsPrerequisite(unittest.TestCase):
    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)

    def test_fold_populates_fts_text(self):
        if not graphstore.fts5_available(self.conn):
            self.skipTest("FTS5 unavailable")
        gather.fold_bundle(self.conn, _crafted_bundle())
        rows = self.conn.execute("SELECT count(*) FROM fts_text").fetchone()[0]
        self.assertGreater(rows, 0)

    def test_fts_search_finds_owning_node(self):
        if not graphstore.fts5_available(self.conn):
            self.skipTest("FTS5 unavailable")
        gather.fold_bundle(self.conn, _crafted_bundle())
        # phrase from PR 1 body -> the PR social node id
        hits = graphstore.fts_search(self.conn, "firewall")
        self.assertIn(graphstore.qualify_id("acme", "r1", "pr-1"), hits)
        # comment body -> still the owning PR node
        hits2 = graphstore.fts_search(self.conn, '"nice work"')
        self.assertIn(graphstore.qualify_id("acme", "r1", "pr-1"), hits2)
        # commit message -> the commit code node
        hits3 = graphstore.fts_search(self.conn, "policy")
        self.assertIn(graphstore.qualify_id("acme", "r1", "c0ffee1"), hits3)

    def test_indexing_idempotent(self):
        if not graphstore.fts5_available(self.conn):
            self.skipTest("FTS5 unavailable")
        b = _crafted_bundle()
        gather.fold_bundle(self.conn, b)
        n1 = self.conn.execute("SELECT count(*) FROM fts_text").fetchone()[0]
        gather.fold_bundle(self.conn, b)  # re-fold
        n2 = self.conn.execute("SELECT count(*) FROM fts_text").fetchone()[0]
        self.assertEqual(n1, n2)


class TestPersonImpact(unittest.TestCase):
    def setUp(self):
        self.conn = graphstore.open_store(":memory:")
        graphstore.init_schema(self.conn)
        gather.fold_bundle(self.conn, _crafted_bundle())

    def test_golden(self):
        res = spotlight.person_impact(self.conn, "acme", "alice")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["query"], "person")
        self.assertEqual(res["login"], "alice")
        self.assertEqual(res["is_bot"], False)
        # areas/modules come from code_graph area attribution (none in this
        # bundle), so they are empty here — the field is present and a list.
        self.assertEqual(res["areas"], [])
        self.assertEqual(res["modules"], [])

        contrib = res["contributions"]
        # grouped by type, each with count + cited rows
        self.assertEqual(contrib["authored"]["count"], 2)  # PR 1 + commit
        self.assertEqual(contrib["reviewed"]["count"], 1)  # PR 2
        self.assertEqual(contrib["commented"]["count"], 1)  # PR 2 comment
        self.assertEqual(contrib["reported"]["count"], 1)  # issue 9
        self.assertNotIn("merged", contrib)  # alice merged nothing

        # citations present on rows
        authored_pr = [r for r in contrib["authored"]["items"]
                       if r["id"].endswith("#pr-1")][0]
        self.assertEqual(authored_pr["number"], 1)
        self.assertEqual(authored_pr["url"],
                         "https://github.com/acme/r1/pull/1")
        self.assertEqual(authored_pr["title"], "Add firewall policy")
        authored_commit = [r for r in contrib["authored"]["items"]
                           if r["id"].endswith("#c0ffee1")][0]
        self.assertEqual(authored_commit["sha"], "c0ffee1")

        # symbols authored + authored_then_removed
        self.assertEqual(len(res["symbols_authored"]), 1)
        self.assertTrue(res["symbols_authored"][0]["id"].endswith(
            "main.bicep#bicep:resource:fw"))
        self.assertEqual(len(res["authored_then_removed"]), 1)

        # trains anchored: the PR1/issue9 closes train
        self.assertEqual(len(res["trains_anchored"]), 1)
        train = res["trains_anchored"][0]
        self.assertIn("anchor", train)
        self.assertGreaterEqual(train["reached"], 2)

    def test_contributions_ordered(self):
        res = spotlight.person_impact(self.conn, "acme", "alice")
        # authored items ordered by (ts, dst_id)
        items = res["contributions"]["authored"]["items"]
        keys = [(r.get("ts") or "", r["id"]) for r in items]
        self.assertEqual(keys, sorted(keys))

    def test_needs_gather_unknown_login(self):
        res = spotlight.person_impact(self.conn, "acme", "nobody")
        self.assertEqual(res["status"], "needs_gather")
        self.assertIn("guidance", res)
        self.assertIn("nobody", res["guidance"])

    def test_determinism(self):
        a = json.dumps(spotlight.person_impact(self.conn, "acme", "alice"),
                       sort_keys=True)
        b = json.dumps(spotlight.person_impact(self.conn, "acme", "alice"),
                       sort_keys=True)
        self.assertEqual(a, b)

    def test_cross_repo_aggregation(self):
        # fold a second repo's window for the same login -> aggregates
        gather.fold_bundle(self.conn, _crafted_bundle(repo="r2"))
        res = spotlight.person_impact(self.conn, "acme", "alice")
        # authored now spans both repos' PR-1 + commit (2 per repo)
        ids = [r["id"] for r in res["contributions"]["authored"]["items"]]
        self.assertTrue(any("/r1#" in i for i in ids))
        self.assertTrue(any("/r2#" in i for i in ids))


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.store = os.path.join(HERE, "workspace", "_spotlight_test.db")
        os.makedirs(os.path.dirname(self.store), exist_ok=True)
        if os.path.exists(self.store):
            os.remove(self.store)
        conn = graphstore.open_store(self.store)
        graphstore.init_schema(conn)
        gather.fold_bundle(conn, _crafted_bundle())
        conn.close()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = self.store + suffix
            if os.path.exists(p):
                os.remove(p)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(HERE, "spotlight.py")] + list(args),
            capture_output=True, text=True)

    def test_default_json(self):
        r = self._run("person", "alice", "--store", self.store)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["login"], "alice")

    def test_needs_gather_exits_zero(self):
        r = self._run("person", "nobody", "--store", self.store)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["status"], "needs_gather")

    def test_md_renders(self):
        r = self._run("person", "alice", "--store", self.store, "--md")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("alice", r.stdout)

    def test_bad_query_exits_nonzero(self):
        r = self._run("bogus", "x", "--store", self.store)
        self.assertNotEqual(r.returncode, 0)

    def test_project_autodetected(self):
        r = self._run("person", "alice", "--store", self.store)
        self.assertEqual(r.returncode, 0, r.stderr)


@unittest.skipUnless(os.path.exists(REAL_STORE), "real store absent")
class TestRealDataSmoke(unittest.TestCase):
    def test_person_impact_real(self):
        conn = graphstore.open_store(REAL_STORE)
        res = spotlight.person_impact(conn, "Azure", "AlexanderSehr")
        conn.close()
        self.assertEqual(res["status"], "ok")
        # non-empty contributions with citations
        total = sum(g["count"] for g in res["contributions"].values())
        self.assertGreater(total, 0)
        # at least one cited row carries a url/number/sha
        any_cite = any(
            ("url" in r or "number" in r or "sha" in r)
            for g in res["contributions"].values() for r in g["items"])
        self.assertTrue(any_cite)


if __name__ == "__main__":
    unittest.main()
